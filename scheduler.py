# scheduler.py
import asyncio
import json
from datetime import datetime, timedelta
from aiogram.types import FSInputFile
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
import api_utils as api
import database as db
from config import UPDATE_INTERVAL, ADMIN_IDS, DB_NAME

# Кеш в пам'яті
schedules_cache = {}
# Історія сповіщень
alert_history = set()

# === НОВЕ: Трекінг стану API для сповіщень адміну ===
_last_known_api_source = None
_last_known_emergency = set()

# Словник відправок: { (region, queue): "2024-01-26" }
sent_notifications = {}


async def smart_broadcast(bot, region, queue, text_blackout, text_light, filter_func):
    """
    Розумна розсилка в ОСОБИСТІ:
    1. Перевіряє налаштування кожного юзера (filter_func).
    2. Відправляє текст залежно від режиму (blackout/light).
    """
    users = await db.get_users_by_queue(region, queue)

    for (uid,) in users:
        try:
            # Отримуємо налаштування юзера
            settings = await db.get_user_settings(uid)

            # Перевіряємо, чи підходить цей юзер під умови розсилки
            if not filter_func(settings):
                continue

            # Вибираємо правильний текст
            mode = settings.get("display_mode", "blackout")
            text_to_send = text_light if mode == "light" else text_blackout

            await bot.send_message(uid, text_to_send, parse_mode="Markdown")
        except (TelegramForbiddenError, TelegramBadRequest):
            await db.mark_user_inactive(uid)
        except Exception:
            pass

        # Невелика затримка, щоб уникнути блокування за флуд
        await asyncio.sleep(0.05)


async def group_broadcast(bot, region, queue, text_blackout, text_light, filter_func):
    """
    Розсилка в ГРУПИ І КАНАЛИ:
    1. Перевіряє налаштування групи (filter_func).
    2. Відправляє текст залежно від режиму (blackout/light).
    """
    groups = await db.get_groups_by_queue(region, queue)

    for group in groups:
        chat_id = group[0]
        settings = {
            "display_mode": group[1] or "blackout",
            "notify_outage": group[2] if group[2] is not None else 1,
            "notify_return": group[3] if group[3] is not None else 1,
            "notify_changes": group[4] if group[4] is not None else 1,
            "notify_morning": group[5] if group[5] is not None else 1,
            "notify_before": group[6] if len(group) > 6 and group[6] is not None else 5,
            "notify_return_before": (
                group[7] if len(group) > 7 and group[7] is not None else 0
            ),
        }

        try:
            if not filter_func(settings):
                continue

            mode = settings.get("display_mode", "blackout")
            text_to_send = text_light if mode == "light" else text_blackout

            await bot.send_message(chat_id, text_to_send, parse_mode="Markdown")
        except Exception:
            pass

        await asyncio.sleep(0.05)


def find_next_outage(current_time_str, today_intervals, tomorrow_intervals):
    """Шукає час наступного відключення."""
    for start, end in today_intervals:
        if start > current_time_str:
            return f"сьогодні о {start}"

    if tomorrow_intervals:
        start, end = tomorrow_intervals[0]
        return f"завтра о {start}"

    return None


async def check_updates(bot):
    """Перевіряє оновлення графіків на сайті."""
    global _last_known_api_source, _last_known_emergency
    first_run = True

    while True:
        try:
            # Очищаємо старі дані статистики
            await db.cleanup_old_stats()

            data = await api.fetch_api_data()

            # === НОВЕ: Трекінг перемикання API та сповіщення адміну ===
            current_source = api.api_state.get("active_source", "primary")
            if (
                _last_known_api_source is not None
                and _last_known_api_source != current_source
            ):
                admin_id = (
                    ADMIN_IDS[0]
                    if isinstance(ADMIN_IDS, list) and ADMIN_IDS
                    else ADMIN_IDS
                )
                source_name = (
                    "🟢 DTEK Proxy (Основне)"
                    if current_source == "primary"
                    else "🟡 Резервне API"
                )
                old_name = (
                    "🟢 DTEK Proxy"
                    if _last_known_api_source == "primary"
                    else "🟡 Резервне"
                )
                try:
                    await bot.send_message(
                        admin_id,
                        f"🔄 **API Failover!**\n\n"
                        f"Було: {old_name}\n"
                        f"Стало: {source_name}\n"
                        f"⏰ {datetime.now().strftime('%d.%m.%Y %H:%M')}",
                        parse_mode="Markdown",
                    )
                except Exception:
                    pass
            _last_known_api_source = current_source

            # === НОВЕ: Сповіщення про екстрені відключення ===
            current_emergency = api.api_state.get("last_emergency_regions", set())
            new_emergency = current_emergency - _last_known_emergency
            if new_emergency and not first_run:
                for region_name in new_emergency:
                    emergency_msg = f"🚨 **ЕКСТРЕНІ ВІДКЛЮЧЕННЯ!**\n📍 {region_name}\n\nВ регіоні діють позапланові відключення."
                    # Розсилка всім юзерам цього регіону
                    subs = await db.get_all_subs()
                    for reg, queue in subs:
                        if reg == region_name:
                            await smart_broadcast(
                                bot,
                                reg,
                                queue,
                                emergency_msg,
                                emergency_msg,
                                lambda s: s["notify_changes"] == 1,
                            )
                            await group_broadcast(
                                bot,
                                reg,
                                queue,
                                emergency_msg,
                                emergency_msg,
                                lambda s: s["notify_changes"] == 1,
                            )
            _last_known_emergency = (
                current_emergency.copy() if current_emergency else set()
            )

            if data:
                today = datetime.now().strftime("%Y-%m-%d")
                tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

                today_nice = datetime.now().strftime("%d.%m")
                tomorrow_nice = (datetime.now() + timedelta(days=1)).strftime("%d.%m")

                subs = await db.get_all_subs()

                for region, queue in subs:
                    r_data = next(
                        (r for r in data["regions"] if r["name_ua"] == region), None
                    )
                    if not r_data:
                        continue

                    today_sch = r_data["schedule"].get(queue, {}).get(today, None)
                    tom_sch = r_data["schedule"].get(queue, {}).get(tomorrow, None)

                    cached = schedules_cache.get((region, queue), {})
                    cached_date = cached.get("date")

                    if cached_date != today:
                        cached_today = None
                        cached_tom = None
                    else:
                        cached_today = cached.get("today")
                        cached_tom = cached.get("tomorrow")

                    # --- 1. ПЕРЕВІРКА СЬОГОДНІ ---
                    if today_sch:
                        await db.save_stats(
                            region, queue, today, api.calculate_off_hours(today_sch)
                        )

                        # ВАЖЛИВО: target_status=2 означає, що ми реагуємо на зміни ГАРАНТОВАНИХ відключень
                        current_norm = api.parse_intervals(today_sch, target_status=2)
                        cached_norm = (
                            api.parse_intervals(cached_today, target_status=2)
                            if cached_today
                            else None
                        )

                        # print(f"[DEBUG] {region}/{queue} | cached_today={cached_today} | today_sch={today_sch}")
                        # print(f"[DEBUG] cached_norm={cached_norm} | current_norm={current_norm}")

                        if cached_norm is not None and json.dumps(
                            current_norm, sort_keys=True
                        ) != json.dumps(cached_norm, sort_keys=True):
                            txt_b = api.format_message(
                                today_sch, queue, today, False, "blackout"
                            )
                            txt_l = api.format_message(
                                today_sch, queue, today, False, "light"
                            )

                            if not first_run:
                                header = f"🔄 📅 **Оновлено графік на СЬОГОДНІ! ({today_nice})**\n"
                                await smart_broadcast(
                                    bot,
                                    region,
                                    queue,
                                    header + txt_b.split("\n", 1)[1],
                                    header + txt_l.split("\n", 1)[1],
                                    lambda s: s["notify_changes"] == 1,
                                )
                                # === НОВЕ: розсилка в групи ===
                                await group_broadcast(
                                    bot,
                                    region,
                                    queue,
                                    header + txt_b.split("\n", 1)[1],
                                    header + txt_l.split("\n", 1)[1],
                                    lambda s: s["notify_changes"] == 1,
                                )
                                # Запам'ятовуємо, що для цієї черги вже було відправлено актуальний графік
                                sent_notifications[(region, queue)] = today

                    # --- 2. ПЕРЕВІРКА ЗАВТРА ---
                    if (tom_sch is not None) and (cached_tom is None):
                        await db.save_stats(
                            region, queue, tomorrow, api.calculate_off_hours(tom_sch)
                        )

                        if not first_run and api.calculate_off_hours(tom_sch) > 0:
                            txt_b = api.format_message(
                                tom_sch, queue, tomorrow, True, "blackout"
                            )
                            txt_l = api.format_message(
                                tom_sch, queue, tomorrow, True, "light"
                            )

                            await smart_broadcast(
                                bot,
                                region,
                                queue,
                                txt_b,
                                txt_l,
                                lambda s: s["notify_changes"] == 1,
                            )
                            # === НОВЕ: розсилка в групи ===
                            await group_broadcast(
                                bot,
                                region,
                                queue,
                                txt_b,
                                txt_l,
                                lambda s: s["notify_changes"] == 1,
                            )

                    elif (tom_sch is not None) and (cached_tom is not None):
                        tom_norm = api.parse_intervals(tom_sch, target_status=2)
                        cached_tom_norm = api.parse_intervals(
                            cached_tom, target_status=2
                        )

                        if json.dumps(tom_norm, sort_keys=True) != json.dumps(
                            cached_tom_norm, sort_keys=True
                        ):
                            await db.save_stats(
                                region,
                                queue,
                                tomorrow,
                                api.calculate_off_hours(tom_sch),
                            )

                            if not first_run:
                                txt_b = api.format_message(
                                    tom_sch, queue, tomorrow, True, "blackout"
                                )
                                txt_l = api.format_message(
                                    tom_sch, queue, tomorrow, True, "light"
                                )

                                if txt_b:
                                    header = f"🔄 🔮 **Оновлено графік на ЗАВТРА! ({tomorrow_nice})**\n"
                                    await smart_broadcast(
                                        bot,
                                        region,
                                        queue,
                                        header + txt_b.split("\n", 1)[1],
                                        header + txt_l.split("\n", 1)[1],
                                        lambda s: s["notify_changes"] == 1,
                                    )
                                    # === НОВЕ: розсилка в групи ===
                                    await group_broadcast(
                                        bot,
                                        region,
                                        queue,
                                        header + txt_b.split("\n", 1)[1],
                                        header + txt_l.split("\n", 1)[1],
                                        lambda s: s["notify_changes"] == 1,
                                    )

                    old_cache = schedules_cache.get((region, queue), {})
                    # Зберігаємо старий кеш тільки якщо вже були дані на СЬОГОДНІ (захист від збою API серед дня)
                    # Якщо новий день — None є нормою, не підміняємо
                    same_day = old_cache.get("date") == today
                    schedules_cache[(region, queue)] = {
                        "date": today,
                        "today": (
                            old_cache.get("today")
                            if same_day and today_sch is None
                            else today_sch
                        ),
                        "tomorrow": (
                            old_cache.get("tomorrow")
                            if same_day and tom_sch is None
                            else tom_sch
                        ),
                    }

                current_date = datetime.now()
                for i in range(7):
                    d = (current_date - timedelta(days=i)).strftime("%Y-%m-%d")
                    if "r_data" in locals() and r_data:
                        sch = r_data["schedule"].get(queue, {}).get(d)
                        if sch:
                            await db.save_stats(
                                region, queue, d, api.calculate_off_hours(sch)
                            )

                if first_run:
                    first_run = False

        except Exception as e:
            print(f"Update Error: {e}")

        await asyncio.sleep(UPDATE_INTERVAL)


async def check_alerts(bot):
    """Щохвилинна перевірка для сповіщень."""
    # global schedule_sent_today # Більше не потрібно

    while True:
        try:
            now = datetime.now()
            curr_time = now.strftime("%H:%M")
            today_str = now.strftime("%Y-%m-%d")

            # --- СБРОС У 00:00 ---
            if curr_time == "00:00":
                alert_history.clear()
                # Очищаємо історію відправок на новий день (опціонально, бо ми перевіряємо дату)
                # sent_notifications.clear()

            # --- НОВЕ: РАНКОВЕ ОПОВІЩЕННЯ (06:00) ---
            if curr_time == "06:00":
                print("☀️ Перевірка ранкового зведення...")
                # Проходимо по всіх відомих чергах в кеші
                for (region, queue), data in schedules_cache.items():
                    # Переконуємось, що дані свіжі
                    if data.get("date") != today_str:
                        continue

                    # Перевіряємо, чи ми вже відправляли графік для цієї конкретної черги сьогодні
                    last_sent = sent_notifications.get((region, queue))
                    if last_sent == today_str:
                        continue  # Вже було оновлення вночі, пропускаємо

                    today_sch = data.get("today")
                    if not today_sch:
                        continue

                    # === НОВА ЛОГІКА: УНИКНЕННЯ СПАМУ ===
                    # Отримуємо статистику
                    today_off = api.calculate_off_hours(today_sch)

                    # Отримуємо вчорашню дату і статистику
                    yesterday = (datetime.now() - timedelta(days=1)).strftime(
                        "%Y-%m-%d"
                    )
                    yesterday_off = await db.get_off_hours_for_date(
                        region, queue, yesterday
                    )

                    # Якщо ВЧОРА було 0 годин відключень, і СЬОГОДНІ теж 0 - пропускаємо
                    # (Щоб не писати кожен день "Світла не вимикають")
                    if today_off == 0 and yesterday_off == 0:
                        # Але ставимо галочку, що ми "обробили" цю чергу, щоб не повертатися
                        sent_notifications[(region, queue)] = today_str
                        continue
                    # ===================================

                    # Формуємо повідомлення
                    txt_b = api.format_message(
                        today_sch, queue, today_str, False, "blackout"
                    )
                    txt_l = api.format_message(
                        today_sch, queue, today_str, False, "light"
                    )

                    # Заголовок
                    header = f"☀️ **Добрий ранок! Графік на сьогодні:**\n"

                    await smart_broadcast(
                        bot,
                        region,
                        queue,
                        header + txt_b.split("\n", 1)[1],
                        header + txt_l.split("\n", 1)[1],
                        lambda s: s["notify_changes"] == 1,
                    )
                    # === НОВЕ: розсилка в групи (ранкове зведення) ===
                    await group_broadcast(
                        bot,
                        region,
                        queue,
                        header + txt_b.split("\n", 1)[1],
                        header + txt_l.split("\n", 1)[1],
                        lambda s: s["notify_changes"] == 1,
                    )

                    # Запам'ятовуємо, що відправили
                    sent_notifications[(region, queue)] = today_str

            # Часові точки для перевірки
            check_moments = {
                5: (now + timedelta(minutes=5)).strftime("%H:%M"),
                10: (now + timedelta(minutes=10)).strftime("%H:%M"),
                15: (now + timedelta(minutes=15)).strftime("%H:%M"),
                30: (now + timedelta(minutes=30)).strftime("%H:%M"),
                60: (now + timedelta(minutes=60)).strftime("%H:%M"),
            }

            for key, data in list(schedules_cache.items()):
                today_sch = data.get("today")
                tom_sch = data.get("tomorrow")

                if not today_sch:
                    continue

                # ВАЖЛИВО: Отримуємо ГАРАНТОВАНІ відключення (status=2)
                today_intervals = api.parse_intervals(today_sch, target_status=2)
                tom_intervals = (
                    api.parse_intervals(tom_sch, target_status=2) if tom_sch else []
                )

                # --- 1. ПЕРЕВІРКА ГАРАНТОВАНИХ (Status 2) ---
                for start, end in today_intervals:
                    # А) СПОВІЩЕННЯ ПРО ВІДКЛЮЧЕННЯ
                    if start != "00:00":
                        for mins, check_time in check_moments.items():
                            if check_time == start:
                                alert_id = f"{key}_{start}_out_pre_{mins}"
                                if alert_id not in alert_history:
                                    actual_end = end
                                    if (
                                        end == "24:00"
                                        and tom_intervals
                                        and tom_intervals[0][0] == "00:00"
                                    ):
                                        actual_end = tom_intervals[0][1]
                                        actual_end = (
                                            "кінця завтрашньої доби (24:00)"
                                            if actual_end == "24:00"
                                            else f"завтра до {actual_end}"
                                        )
                                    elif end == "24:00":
                                        actual_end = "24:00"

                                    msg = f"⏳ **Скоро відключення (через {mins} хв).**\nСвітла не буде до **{actual_end}**."

                                    await smart_broadcast(
                                        bot,
                                        key[0],
                                        key[1],
                                        msg,
                                        msg,
                                        lambda s, m=mins: s["notify_outage"] == 1
                                        and s["notify_before"] == m,
                                    )
                                    # === НОВЕ: розсилка в групи ===
                                    await group_broadcast(
                                        bot,
                                        key[0],
                                        key[1],
                                        msg,
                                        msg,
                                        lambda s, m=mins: s["notify_outage"] == 1
                                        and s["notify_before"] == m,
                                    )
                                    alert_history.add(alert_id)

                    # Б) СПОВІЩЕННЯ ПРО ВКЛЮЧЕННЯ
                    if end != "24:00":
                        for mins, check_time in check_moments.items():
                            if check_time == end:
                                alert_id = f"{key}_{end}_ret_pre_{mins}"
                                if alert_id not in alert_history:
                                    msg = f"💡 **Світло з'явиться орієнтовно через {mins} хв (о {end}).**"

                                    await smart_broadcast(
                                        bot,
                                        key[0],
                                        key[1],
                                        msg,
                                        msg,
                                        lambda s, m=mins: s["notify_return"] == 1
                                        and s["notify_return_before"] == m,
                                    )
                                    # === НОВЕ: розсилка в групи ===
                                    await group_broadcast(
                                        bot,
                                        key[0],
                                        key[1],
                                        msg,
                                        msg,
                                        lambda s, m=mins: s["notify_return"] == 1
                                        and s["notify_return_before"] == m,
                                    )
                                    alert_history.add(alert_id)

                # --- 2. МОЖЛИВІ (3) - СПОВІЩЕННЯ ПРО ПОЧАТОК ---
                # Отримуємо інтервали можливих відключень
                intervals_possible = api.parse_intervals(today_sch, target_status=3)
                for start, end in intervals_possible:
                    if start != "00:00":
                        for mins, check_time in check_moments.items():
                            if check_time == start:
                                alert_id = f"{key}_{start}_poss_pre_{mins}"
                                if alert_id not in alert_history:
                                    msg = f"⚠️ **Увага! Через {mins} хв можливе відключення.**\nСіра зона графіку (до {end})."

                                    # Використовуємо налаштування notify_outage (або можна створити окреме)
                                    # Тут поки що прив'язано до сповіщень про відключення
                                    await smart_broadcast(
                                        bot,
                                        key[0],
                                        key[1],
                                        msg,
                                        msg,
                                        lambda s, m=mins: s["notify_outage"] == 1
                                        and s["notify_before"] == m,
                                    )
                                    # === НОВЕ: розсилка в групи ===
                                    await group_broadcast(
                                        bot,
                                        key[0],
                                        key[1],
                                        msg,
                                        msg,
                                        lambda s, m=mins: s["notify_outage"] == 1
                                        and s["notify_before"] == m,
                                    )
                                    alert_history.add(alert_id)

                # --- 3. Стик днів (23:XX -> 00:00) ---
                if tom_intervals and tom_intervals[0][0] == "00:00":
                    start_tom, end_tom = tom_intervals[0]
                    for mins, check_time in check_moments.items():
                        if check_time == "00:00":
                            alert_id = f"{key}_00:00_tom_pre_{mins}"
                            if alert_id not in alert_history:
                                end_display = "24:00" if end_tom == "24:00" else end_tom
                                msg = f"⏳ **Скоро відключення (через {mins} хв, о 00:00).**\nСвітла не буде до **{end_display}**."

                                await smart_broadcast(
                                    bot,
                                    key[0],
                                    key[1],
                                    msg,
                                    msg,
                                    lambda s, m=mins: s["notify_outage"] == 1
                                    and s["notify_before"] == m,
                                )
                                # === НОВЕ: розсилка в групи ===
                                await group_broadcast(
                                    bot,
                                    key[0],
                                    key[1],
                                    msg,
                                    msg,
                                    lambda s, m=mins: s["notify_outage"] == 1
                                    and s["notify_before"] == m,
                                )
                                alert_history.add(alert_id)

                # --- 4. СПОВІЩЕННЯ В МОМЕНТ ВКЛЮЧЕННЯ (Тільки після гарантованих) ---
                for start, end in today_intervals:
                    if curr_time == end and end != "24:00":
                        alert_id = f"{key}_{end}_on"
                        if alert_id not in alert_history:
                            next_outage = find_next_outage(
                                end, today_intervals, tom_intervals
                            )
                            next_info = (
                                f"Наступне відключення: **{next_outage}**."
                                if next_outage
                                else "✅ Далі без відключень."
                            )

                            msg = (
                                f"⚡️ **Світло повертається!**\n"
                                f"Включення за графіком ({end}).\n"
                                f"{next_info}"
                            )

                            await smart_broadcast(
                                bot,
                                key[0],
                                key[1],
                                msg,
                                msg,
                                lambda s: s["notify_return"] == 1,
                            )
                            # === НОВЕ: розсилка в групи ===
                            await group_broadcast(
                                bot,
                                key[0],
                                key[1],
                                msg,
                                msg,
                                lambda s: s["notify_return"] == 1,
                            )
                            alert_history.add(alert_id)

        except Exception as e:
            print(f"Alert Error: {e}")

        await asyncio.sleep(60 - datetime.now().second)


# === НОВЕ: ФОНОВА ЗАДАЧА ДЛЯ БЕКАПУ ===
async def auto_backup(bot):
    """Щодня о 03:00 відправляє базу даних адміну."""
    print("💾 Система бекапів запущена. Чекаю 03:00...")
    while True:
        try:
            now = datetime.now()
            # Встановлюємо час на 03:00 сьогодні
            target_time = now.replace(hour=3, minute=0, second=0, microsecond=0)

            # Якщо 03:00 вже минуло, плануємо на завтра
            if now >= target_time:
                target_time += timedelta(days=1)

            # Рахуємо скільки спати
            wait_seconds = (target_time - now).total_seconds()

            # Спимо до 03:00
            await asyncio.sleep(wait_seconds)

            # --- ВІДПРАВКА БЕКАПУ ---
            # Беремо першого адміна зі списку, якщо це список, або сам ID, якщо це число
            admin_id = (
                ADMIN_IDS[0] if isinstance(ADMIN_IDS, list) and ADMIN_IDS else ADMIN_IDS
            )

            import os
            if not DB_NAME or not os.path.isfile(DB_NAME):
                await asyncio.sleep(60)
                continue
            db_file = FSInputFile(DB_NAME)
            caption = f"📦 **Автоматичний бекап бази даних**\n📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}"

            try:
                await bot.send_document(
                    admin_id, db_file, caption=caption, parse_mode="Markdown"
                )
                print("✅ Бекап успішно відправлено!")
            except Exception as e:
                print(f"Помилка відправки файлу: {e}")

            # Спимо трохи, щоб не відправити двічі в ту саму секунду (хоча timedelta захищає)
            await asyncio.sleep(60)

        except Exception as e:
            print(f"Backup Error: {e}")
            await asyncio.sleep(300)  # Якщо помилка, пробуємо через 5 хв
