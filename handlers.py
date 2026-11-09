# handlers.py
from datetime import datetime, timedelta
import asyncio
from aiogram import Router, types, F
from aiogram.filters import (
    Command,
    CommandObject,
    ChatMemberUpdatedFilter,
    IS_MEMBER,
    IS_NOT_MEMBER,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
from aiogram.types import KeyboardButton, InlineKeyboardButton, ChatMemberUpdated
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest

import database as db
import api_utils as api
import scheduler
from config import ADMIN_IDS, BOT_TOKEN  # Імпортуємо список адмінів

# Для сумісності з вашим старим кодом, якщо ADMIN_ID використовується як число
ADMIN_ID = ADMIN_IDS[0] if isinstance(ADMIN_IDS, list) and ADMIN_IDS else 723550550

router = Router()

# Витягуємо username бота з токену для посилання "Додати в групу"
_bot_username_cache = None


async def get_bot_username(bot):
    global _bot_username_cache
    if _bot_username_cache is None:
        me = await bot.get_me()
        _bot_username_cache = me.username
    return _bot_username_cache


# --- ДОПОМІЖНА ФУНКЦІЯ ЧАСУ ---
def get_local_now():
    """Повертає поточний час."""
    return datetime.now()


def get_main_keyboard(user_id=None):
    """Створює нижнє меню. Якщо user_id == ADMIN_ID, додає кнопку панелі."""
    kb = ReplyKeyboardBuilder()
    kb.row(
        KeyboardButton(text="📅 Графік на сьогодні"),
        KeyboardButton(text="🔮 Графік на завтра"),
    )
    kb.row(KeyboardButton(text="📊 Аналітика"), KeyboardButton(text="⚙️ Налаштування"))
    kb.row(KeyboardButton(text="💬 Підтримка"))

    # Додаємо кнопку тільки Адміну
    if user_id == ADMIN_ID or (isinstance(ADMIN_IDS, list) and user_id in ADMIN_IDS):
        kb.row(KeyboardButton(text="👨‍💼 Адмін-панель"))

    return kb.as_markup(resize_keyboard=True)


@router.message(Command("start"))
async def start_command(message: types.Message, command: CommandObject):
    """Команда /start."""
    # В групах /start не працює — вітання виконує my_chat_member
    if message.chat.type in ["group", "supergroup"]:
        return

    # Підтримка Deep Linking
    if command.args:
        if command.args == "settings":
            user = await db.get_user(message.from_user.id)
            if user:
                await show_settings_main(message, message.from_user.id)
                return
        elif command.args.startswith("c"):
            # Налаштування каналу
            chan_id = int(command.args.replace("c", "-100"))
            if not await is_admin(message.bot, chan_id, message.from_user.id):
                await message.answer("⛔ Ви не є адміністратором цього каналу.")
                return

            group_sub = await db.get_group_sub(chan_id)
            if group_sub:
                await show_group_settings_menu(message, chan_id)
            else:
                await send_group_region_menu(message, chan_id)
            return

    # Перевіряємо, чи знаємо ми цього юзера
    user = await db.get_user(message.from_user.id)

    if user:
        await db.mark_user_active(message.from_user.id)
        welcome_text = f"👋 **Ласкаво просимо назад!**\n📍 Ваш вибір: **{user[0]}, Черга {user[1]}**"

        # Інлайн-кнопка для додавання бота в групу
        bot_username = await get_bot_username(message.bot)
        kb_inline = InlineKeyboardBuilder()
        kb_inline.button(
            text="➕ Додати бота в групу/канал",
            url=f"https://t.me/{bot_username}?startgroup=true&admin=post_messages",
        )

        await message.answer(
            welcome_text,
            reply_markup=get_main_keyboard(message.from_user.id),
            parse_mode="Markdown",
        )
        await message.answer(
            "💡 Хочете отримувати графік у групі або каналі?",
            reply_markup=kb_inline.as_markup(),
        )
        return

    # Якщо юзера немає в базі
    text = "👋 **Вітаю! Це бот Моніторингу Світла.**\n" "👇 **Оберіть вашу область:**"
    await show_regions_menu(message, text)


async def is_admin(bot, chat_id, user_id):
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ["creator", "administrator"]
    except Exception:
        return False


# === КОМАНДА /addtogroup ===
@router.message(Command("addtogroup"))
async def addtogroup_command(message: types.Message):
    """Показує кнопку для додавання бота в групу/канал."""
    if message.chat.type != "private":
        return

    bot_username = await get_bot_username(message.bot)
    kb = InlineKeyboardBuilder()
    kb.button(
        text="➕ Додати бота в групу",
        url=f"https://t.me/{bot_username}?startgroup=true&admin=post_messages",
    )
    kb.button(
        text="📡 Додати бота в канал",
        url=f"https://t.me/{bot_username}?startchannel=true&admin=post_messages",
    )
    kb.adjust(1)

    await message.answer(
        "👥 **Додати бота в групу або канал**\n\n"
        "Оберіть куди додати:\n"
        "• **Група** — бот буде слати сповіщення і відповідати на команди\n"
        "• **Канал** — бот буде публікувати сповіщення\n\n"
        "Після додавання використовуйте /setup в групі для налаштування.",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown",
    )


# === НОВА КОМАНДА /grafik ===
@router.message(Command("grafik"))
async def grafik_command(message: types.Message):
    """Виводить графік на сьогодні для користувача або групи."""
    # Якщо в групі — спочатку перевіряємо підписку групи
    if message.chat.type in ["group", "supergroup"]:
        group_sub = await db.get_group_sub(message.chat.id)
        if group_sub:
            # Група налаштована — показуємо графік групи
            region, queue = group_sub[0], group_sub[1]
            display_mode = group_sub[2] or "blackout"
            await show_today_schedule(
                message,
                region,
                queue,
                user_id=message.from_user.id,
                display_mode_override=display_mode,
            )
            return
        # Група НЕ налаштована — fallback на особисті налаштування юзера

    # В особистих або якщо група не налаштована — беремо дані юзера
    user = await db.get_user(message.from_user.id)
    if not user:
        if message.chat.type in ["group", "supergroup"]:
            await message.reply(
                "⚠️ Я не знаю вашого регіону.\nНапишіть мені /start в особисті, або адмін може налаштувати групу через /setup"
            )
        else:
            await message.answer("Спочатку зробіть налаштування через /start.")
        return

    await show_today_schedule(message, user[0], user[1], user_id=message.from_user.id)


# ==========================================
# === РОБОТА З ГРУПАМИ І КАНАЛАМИ ===
# ==========================================


# --- ПРИВІТНЕ ПОВІДОМЛЕННЯ ПРИ ДОДАВАННІ В ГРУПУ ---
@router.my_chat_member(
    ChatMemberUpdatedFilter(member_status_changed=IS_NOT_MEMBER >> IS_MEMBER)
)
async def bot_added_to_group(event: ChatMemberUpdated):
    """Бот доданий в групу або канал — вітальне повідомлення."""
    chat = event.chat

    if chat.type == "channel":
        bot_username = await get_bot_username(event.bot)
        chan_id = str(chat.id).replace("-100", "c")
        kb = InlineKeyboardBuilder()
        kb.button(
            text="⚙️ Налаштувати канал",
            url=f"https://t.me/{bot_username}?start={chan_id}",
        )
        try:
            await event.bot.send_message(
                chat.id,
                "📡 **Бот Моніторингу Світла підключений!**\n\n"
                "Для налаштування перейдіть за посиланням нижче:",
                reply_markup=kb.as_markup(),
                parse_mode="Markdown",
            )
        except Exception:
            pass
        return

    # Для груп — повне привітне повідомлення
    try:
        await event.bot.send_message(
            chat.id,
            "👋 **Привіт! Я бот Моніторингу Світла.**\n\n"
            "Я можу надсилати в цю групу:\n"
            "⚡️ Сповіщення про відключення та включення\n"
            "📅 Щоденний графік світла\n"
            "🔄 Оповіщення при зміні графіку\n"
            "☀️ Ранкове зведення о 06:00\n\n"
            "**Як налаштувати:**\n"
            "1️⃣ /setup — обрати область і чергу для групи\n"
            "2️⃣ /group\\_settings — налаштувати сповіщення\n\n"
            "**Інші команди:**\n"
            "📅 /grafik — графік на сьогодні\n\n"
            "💡 _Налаштувати може тільки адмін групи._",
            parse_mode="Markdown",
        )
    except Exception as e:
        print(f"Помилка привітання в групі: {e}")


# --- КОМАНДА /setup ДЛЯ ГРУПИ ---
async def send_group_region_menu(message_or_callback, target_chat_id):
    """Показує меню вибору регіону для групи/каналів. Може викликатись з групи або в особистих."""
    data = await api.fetch_api_data()
    if not data:
        if isinstance(message_or_callback, types.Message):
            await message_or_callback.answer("⚠️ Помилка отримання даних.")
        else:
            await message_or_callback.message.answer("⚠️ Помилка отримання даних.")
        return

    kb = InlineKeyboardBuilder()
    for idx, region in enumerate(data["regions"]):
        kb.button(
            text=region["name_ua"], callback_data=f"grp_reg|{target_chat_id}|{idx}"
        )
    kb.adjust(2)

    text = "⚙️ **Налаштування групи/каналу**\n\n" "👇 Оберіть область:"
    if isinstance(message_or_callback, types.Message):
        await message_or_callback.answer(
            text, reply_markup=kb.as_markup(), parse_mode="Markdown"
        )
    else:
        await message_or_callback.message.edit_text(
            text, reply_markup=kb.as_markup(), parse_mode="Markdown"
        )


@router.message(Command("setup"))
async def setup_group_command(message: types.Message):
    """Налаштування бота для групи — вибір регіону і черги."""
    if message.chat.type not in ["group", "supergroup"]:
        await message.answer(
            "ℹ️ Ця команда працює тільки в групах.\nДля особистих налаштувань використовуйте /start"
        )
        return

    if not await is_admin(message.bot, message.chat.id, message.from_user.id):
        await message.reply("⛔ Тільки адміністратор групи може налаштовувати бота.")
        return

    await send_group_region_menu(message, message.chat.id)


@router.callback_query(F.data.startswith("grp_reg|"))
async def grp_select_region(callback: types.CallbackQuery):
    """Вибір регіону для групи."""
    parts = callback.data.split("|")
    target_chat_id = int(parts[1])
    region_idx = int(parts[2])

    if not await is_admin(callback.bot, target_chat_id, callback.from_user.id):
        await callback.answer("⛔ Тільки адмін!", show_alert=True)
        return

    data = await api.fetch_api_data()
    if not data or "regions" not in data or region_idx >= len(data["regions"]):
        await callback.answer("Помилка API", show_alert=True)
        return

    region_data = data["regions"][region_idx]
    region_name = region_data["name_ua"]

    # Показуємо першу сторінку черг
    await show_grp_queue_page(
        callback, target_chat_id, region_idx, region_name, region_data, page=0
    )


async def show_grp_queue_page(
    callback, target_chat_id, region_idx, region_name, region_data, page=0
):
    """Показує сторінку черг для групи (пагінація по 12)."""
    QUEUES_PER_PAGE = 12

    sch = region_data.get("schedule")
    if not sch:
        await callback.answer("Черги не знайдено", show_alert=True)
        return

    queues = sorted(
        sch.keys(),
        key=lambda x: [int(p) for p in x.split(".")] if "." in x else [int(x), 0],
    )
    total_pages = (len(queues) + QUEUES_PER_PAGE - 1) // QUEUES_PER_PAGE
    page = max(0, min(page, total_pages - 1))
    start = page * QUEUES_PER_PAGE
    page_queues = queues[start : start + QUEUES_PER_PAGE]

    kb = InlineKeyboardBuilder()
    for q in page_queues:
        kb.button(
            text=f"Черга {q}", callback_data=f"grp_q|{target_chat_id}|{region_idx}|{q}"
        )
    kb.adjust(3)

    # Навігація по сторінках
    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="◀️ Назад",
                    callback_data=f"grp_qpage|{target_chat_id}|{region_idx}|{page-1}",
                )
            )
        nav_buttons.append(
            InlineKeyboardButton(
                text=f"📄 {page+1}/{total_pages}", callback_data="noop"
            )
        )
        if page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="Далі ▶️",
                    callback_data=f"grp_qpage|{target_chat_id}|{region_idx}|{page+1}",
                )
            )
        kb.row(*nav_buttons)

    text = f"📍 **{region_name}**. Оберіть чергу для групи/каналу:"
    if total_pages > 1:
        text += f"\n📄 Сторінка {page+1} з {total_pages} ({len(queues)} черг)"

    await callback.message.edit_text(
        text, reply_markup=kb.as_markup(), parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("grp_qpage|"))
async def grp_queue_page_nav(callback: types.CallbackQuery):
    """Навігація по сторінках черг для групи."""
    parts = callback.data.split("|")
    target_chat_id = int(parts[1])
    region_idx = int(parts[2])
    page = int(parts[3])

    data = await api.fetch_api_data()
    if not data or "regions" not in data or region_idx >= len(data["regions"]):
        await callback.answer("Помилка API", show_alert=True)
        return

    region_data = data["regions"][region_idx]
    region_name = region_data["name_ua"]
    await show_grp_queue_page(
        callback, target_chat_id, region_idx, region_name, region_data, page=page
    )


@router.callback_query(F.data.startswith("grp_q|"))
async def grp_select_queue(callback: types.CallbackQuery):
    """Вибір черги для групи."""
    parts = callback.data.split("|")
    target_chat_id = int(parts[1])
    region_idx = int(parts[2])
    queue = parts[3]

    if not await is_admin(callback.bot, target_chat_id, callback.from_user.id):
        await callback.answer("⛔ Тільки адмін!", show_alert=True)
        return

    data = await api.fetch_api_data()
    if not data or "regions" not in data or region_idx >= len(data["regions"]):
        await callback.answer("Помилка API", show_alert=True)
        return

    region_name = data["regions"][region_idx]["name_ua"]

    # Зберігаємо підписку групи. `added_by` = callback.from_user.id
    chat_title = "Без назви"
    chat_type = "supergroup"
    if callback.message.chat.id == target_chat_id:
        chat_title = callback.message.chat.title or "Без назви"
        chat_type = callback.message.chat.type

    await db.save_group_sub(
        chat_id=target_chat_id,
        chat_title=chat_title,
        chat_type=chat_type,
        region=region_name,
        queue=queue,
        added_by=callback.from_user.id,
    )

    bot_username = await get_bot_username(callback.bot)

    await callback.message.edit_text(
        f"✅ **Групу/Канал налаштовано!**\n\n"
        f"📍 {region_name}, Черга {queue}\n\n"
        f"Тепер бот буде надсилати сповіщення.\n"
        f"Щоб змінити налаштування: /group\\_settings у групі або перейдіть за цим посиланням у випадку каналу:\n"
        f"https://t.me/{bot_username}?start=c{str(target_chat_id).replace('-100', '')}",
        parse_mode="Markdown",
    )

    # === НОВЕ: Надсилаємо актуальний графік у групу/канал ===
    try:
        settings = await db.get_group_settings(target_chat_id)
        display_mode = settings.get("display_mode", "blackout")

        today = get_local_now().strftime("%Y-%m-%d")
        schedule = None

        cached_data = scheduler.schedules_cache.get((region_name, queue))
        if cached_data is not None:
            schedule = cached_data.get("today")
        else:
            api_data = await api.fetch_api_data()
            if api_data:
                for r in api_data["regions"]:
                    if r["name_ua"] == region_name:
                        schedule = r["schedule"].get(queue, {}).get(today)
                        break

        if schedule:
            text = api.format_message(
                schedule, queue, today, is_tomorrow=False, display_mode=display_mode
            )
            header = f"📋 **Актуальний графік на сьогодні:**\n"
            await callback.bot.send_message(
                target_chat_id, header + text, parse_mode="Markdown"
            )
    except Exception as e:
        print(f"Помилка відправки графіку в групу: {e}")


# --- КОМАНДА /group_settings ---
@router.message(Command("group_settings"))
async def group_settings_command(message: types.Message):
    """Налаштування сповіщень для групи."""
    if message.chat.type not in ["group", "supergroup"]:
        await message.answer("ℹ️ Ця команда працює тільки в групах.")
        return

    # Перевірка адміна
    try:
        member = await message.bot.get_chat_member(
            message.chat.id, message.from_user.id
        )
        if member.status not in ["creator", "administrator"]:
            await message.reply(
                "⛔ Тільки адміністратор групи може змінювати налаштування."
            )
            return
    except Exception:
        await message.reply("⚠️ Не вдалося перевірити права.")
        return

    group_sub = await db.get_group_sub(message.chat.id)
    if not group_sub:
        await message.answer("⚠️ Спочатку налаштуйте групу через /setup")
        return

    await show_group_settings_menu(message, message.chat.id)


async def show_group_settings_menu(message, chat_id, edit=False):
    """Показує головне меню налаштувань групи (ідентично особистим налаштуванням)."""
    group_sub = await db.get_group_sub(chat_id)
    if not group_sub:
        return

    region, queue = group_sub[0], group_sub[1]
    settings = await db.get_group_settings(chat_id)

    if settings["display_mode"] == "light":
        mode_status = "🟢 Показую, коли світло Є"
    else:
        mode_status = "⬛️ Показую, коли світла НЕМАЄ"

    t_out = (
        f"{settings['notify_before']} хв"
        if settings["notify_before"] > 0
        else "Вимкнено"
    )
    t_in = (
        f"{settings['notify_return_before']} хв"
        if settings["notify_return_before"] > 0
        else "Вимкнено"
    )

    text = (
        f"⚙️ **Головні налаштування групи**\n"
        f"📍 Локація: **{region}, Черга {queue}**\n\n"
        f"⏰ Таймер відключення: **{t_out}**\n"
        f"⏰ Таймер включення: **{t_in}**\n"
        f"🎨 Вигляд графіку: **{mode_status}**"
    )

    kb = InlineKeyboardBuilder()
    kb.button(
        text="⏰ Налаштувати таймери >", callback_data=f"grp_menu_time_select|{chat_id}"
    )
    kb.button(
        text="🔔 Налаштування сповіщень >", callback_data=f"grp_menu_types|{chat_id}"
    )
    kb.button(text="🎨 Вигляд графіку >", callback_data=f"grp_menu_mode|{chat_id}")
    kb.button(
        text="📍 Змінити область/чергу >", callback_data=f"grp_change_region|{chat_id}"
    )
    kb.button(text="🔕 Відключити бота від групи", callback_data=f"grp_unsub|{chat_id}")

    if message.chat.type == "private":
        kb.button(text="🔙 Назад до списку", callback_data="menu_my_groups")
    else:
        kb.button(text="❌ Закрити меню", callback_data=f"grp_close|{chat_id}")

    kb.adjust(1)

    if edit:
        await message.edit_text(
            text, reply_markup=kb.as_markup(), parse_mode="Markdown"
        )
    else:
        await message.answer(text, reply_markup=kb.as_markup(), parse_mode="Markdown")


# --- ПІДМЕНЮ: ТИПИ СПОВІЩЕНЬ ГРУПИ ---
async def show_group_types_menu(message, chat_id):
    """Підменю сповіщень групи (ідентично особистому)."""
    settings = await db.get_group_settings(chat_id)

    text = f"🔔 **Налаштування сповіщень**\n\n" f"Увімкніть або вимкніть повідомлення:"

    kb = InlineKeyboardBuilder()

    icon_out = "✅" if settings["notify_outage"] else "❌"
    kb.button(
        text=f"{icon_out} Коли зникає світло",
        callback_data=f"grp_tog|{chat_id}|notify_outage",
    )

    icon_ret = "✅" if settings["notify_return"] else "❌"
    kb.button(
        text=f"{icon_ret} Коли з'являється світло",
        callback_data=f"grp_tog|{chat_id}|notify_return",
    )

    icon_chg = "✅" if settings["notify_changes"] else "❌"
    kb.button(
        text=f"{icon_chg} Якщо змінився графік",
        callback_data=f"grp_tog|{chat_id}|notify_changes",
    )

    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data=f"grp_menu_main|{chat_id}")
    )

    await message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")


# --- ПІДМЕНЮ: ВИГЛЯД ГРАФІКУ ГРУПИ ---
async def show_group_mode_menu(message, chat_id):
    """Підменю вигляду графіку групи (ідентично особистому)."""
    settings = await db.get_group_settings(chat_id)
    current = settings["display_mode"]

    text = f"🎨 **Вигляд графіку**\n\n" f"Що показувати на картинці?"

    kb = InlineKeyboardBuilder()

    mark_b = "✅" if current == "blackout" else ""
    kb.button(
        text=f"{mark_b} ⬛️ Коли світла НЕМАЄ",
        callback_data=f"grp_set_mode|{chat_id}|blackout",
    )

    mark_l = "✅" if current == "light" else ""
    kb.button(
        text=f"{mark_l} 🟢 Коли світло Є", callback_data=f"grp_set_mode|{chat_id}|light"
    )

    kb.adjust(1)
    kb.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data=f"grp_menu_main|{chat_id}")
    )

    await message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")


# --- ПІДМЕНЮ: ВИБІР ТАЙМЕРА ГРУПИ (НОВЕ) ---
async def show_group_time_type_selection(message, chat_id):
    """Меню вибору: який таймер налаштовуємо для групи?"""
    text = "⏰ **Налаштування часу**\n\nЯкий таймер ви хочете змінити?"

    kb = InlineKeyboardBuilder()
    kb.button(text="🔦 Відключення", callback_data=f"grp_time_edit|{chat_id}|outage")
    kb.button(text="💡 До включення", callback_data=f"grp_time_edit|{chat_id}|return")
    kb.row(
        InlineKeyboardButton(text="🔙 Назад", callback_data=f"grp_menu_main|{chat_id}")
    )

    await message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")


# --- ПІДМЕНЮ: ВИБІР ХВИЛИН ГРУПИ ---
async def show_group_minutes_menu(message, chat_id, timer_type):
    """Меню вибору хвилин для конкретного таймера групи."""
    settings = await db.get_group_settings(chat_id)

    if timer_type == "outage":
        current = settings["notify_before"]
        title = "🔦 **Попередження про ВІДКЛЮЧЕННЯ (Ця Група)**"
    else:
        current = settings["notify_return_before"]
        title = "💡 **Попередження про ВКЛЮЧЕННЯ (Ця Група)**"

    text = f"{title}\n\n" f"За скільки хвилин попередити учасників?"

    kb = InlineKeyboardBuilder()
    times = [5, 15, 30, 60]

    for t in times:
        mark = "✅" if current == t else ""
        label = "1 год" if t == 60 else f"{t} хв"
        kb.button(
            text=f"{mark} {label}",
            callback_data=f"grp_set_time|{chat_id}|{timer_type}|{t}",
        )

    mark_off = "✅" if current == 0 else ""
    kb.row(
        InlineKeyboardButton(
            text=f"{mark_off} 🔕 Не нагадувати",
            callback_data=f"grp_set_time|{chat_id}|{timer_type}|0",
        )
    )

    kb.adjust(2, 2, 1)
    kb.row(
        InlineKeyboardButton(
            text="🔙 Назад", callback_data=f"grp_menu_time_select|{chat_id}"
        )
    )

    await message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")


# --- КОЛБЕКИ: НАВІГАЦІЯ МЕНЮ ГРУПИ ---
@router.callback_query(F.data.startswith("grp_menu_main|"))
async def grp_go_to_main(callback: types.CallbackQuery):
    """Повернення на головну сторінку налаштувань."""
    target_chat_id = int(callback.data.split("|")[1])
    if not await is_admin(callback.bot, target_chat_id, callback.from_user.id):
        await callback.answer("⛔ Тільки адмін!", show_alert=True)
        return

    await show_group_settings_menu(callback.message, target_chat_id, edit=True)
    await callback.answer()


@router.callback_query(F.data.startswith("grp_menu_types|"))
async def grp_go_to_types(callback: types.CallbackQuery):
    """Перехід до підменю сповіщень."""
    target_chat_id = int(callback.data.split("|")[1])
    if not await is_admin(callback.bot, target_chat_id, callback.from_user.id):
        await callback.answer("⛔ Тільки адмін!", show_alert=True)
        return

    await show_group_types_menu(callback.message, target_chat_id)
    await callback.answer()


@router.callback_query(F.data.startswith("grp_menu_mode|"))
async def grp_go_to_mode(callback: types.CallbackQuery):
    """Перехід до підменю вигляду."""
    target_chat_id = int(callback.data.split("|")[1])
    if not await is_admin(callback.bot, target_chat_id, callback.from_user.id):
        await callback.answer("⛔ Тільки адмін!", show_alert=True)
        return

    await show_group_mode_menu(callback.message, target_chat_id)
    await callback.answer()


@router.callback_query(F.data.startswith("grp_menu_time_select|"))
async def grp_go_to_time_sel(callback: types.CallbackQuery):
    """Перехід до підменю вибору таймера."""
    target_chat_id = int(callback.data.split("|")[1])
    if not await is_admin(callback.bot, target_chat_id, callback.from_user.id):
        await callback.answer("⛔ Тільки адмін!", show_alert=True)
        return
    await show_group_time_type_selection(callback.message, target_chat_id)
    await callback.answer()


@router.callback_query(F.data.startswith("grp_time_edit|"))
async def grp_go_to_time_edit(callback: types.CallbackQuery):
    """Перехід до налаштування конкретного таймера."""
    parts = callback.data.split("|")
    target_chat_id = int(parts[1])
    timer_type = parts[2]

    if not await is_admin(callback.bot, target_chat_id, callback.from_user.id):
        await callback.answer("⛔ Тільки адмін!", show_alert=True)
        return

    await show_group_minutes_menu(callback.message, target_chat_id, timer_type)
    await callback.answer()


@router.callback_query(F.data.startswith("grp_set_time|"))
async def grp_set_time(callback: types.CallbackQuery):
    """Збереження часу таймера."""
    parts = callback.data.split("|")
    target_chat_id = int(parts[1])
    timer_type = parts[2]
    mins = int(parts[3])

    if not await is_admin(callback.bot, target_chat_id, callback.from_user.id):
        await callback.answer("⛔ Тільки адмін!", show_alert=True)
        return

    db_key = "notify_before" if timer_type == "outage" else "notify_return_before"
    await db.update_group_setting(target_chat_id, db_key, mins)

    await show_group_minutes_menu(callback.message, target_chat_id, timer_type)
    await callback.answer()


@router.callback_query(F.data.startswith("grp_close|"))
async def grp_close_menu(callback: types.CallbackQuery):
    """Закрити меню налаштувань."""
    await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data.startswith("grp_tog|"))
async def grp_toggle_setting(callback: types.CallbackQuery):
    """Перемикає налаштування групи."""
    parts = callback.data.split("|")
    target_chat_id = int(parts[1])
    key = parts[2]

    if not await is_admin(callback.bot, target_chat_id, callback.from_user.id):
        await callback.answer("⛔ Тільки адмін!", show_alert=True)
        return

    settings = await db.get_group_settings(target_chat_id)
    new_val = 0 if settings[key] else 1
    await db.update_group_setting(target_chat_id, key, new_val)
    # Після toggle повертаємось в підменю сповіщень
    await show_group_types_menu(callback.message, target_chat_id)
    await callback.answer()


@router.callback_query(F.data.startswith("grp_set_mode|"))
async def grp_set_mode(callback: types.CallbackQuery):
    """Встановлює режим відображення для групи."""
    parts = callback.data.split("|")
    target_chat_id = int(parts[1])
    new_mode = parts[2]

    if not await is_admin(callback.bot, target_chat_id, callback.from_user.id):
        await callback.answer("⛔ Тільки адмін!", show_alert=True)
        return

    await db.update_group_setting(target_chat_id, "display_mode", new_mode)
    await show_group_mode_menu(callback.message, target_chat_id)
    await callback.answer()


@router.callback_query(F.data.startswith("grp_change_region|"))
async def grp_change_region(callback: types.CallbackQuery):
    """Змінити область/чергу для групи."""
    target_chat_id = int(callback.data.split("|")[1])

    if not await is_admin(callback.bot, target_chat_id, callback.from_user.id):
        await callback.answer("⛔ Тільки адмін!", show_alert=True)
        return

    await send_group_region_menu(callback, target_chat_id)
    await callback.answer()


@router.callback_query(F.data.startswith("grp_unsub|"))
async def grp_unsubscribe(callback: types.CallbackQuery):
    """Відписка групи від сповіщень."""
    target_chat_id = int(callback.data.split("|")[1])

    if not await is_admin(callback.bot, target_chat_id, callback.from_user.id):
        await callback.answer("⛔ Тільки адмін!", show_alert=True)
        return

    await db.delete_group_sub(target_chat_id)
    await callback.message.edit_text(
        "🔕 **Бот відключений від групи/каналу.**\n"
        "Сповіщення більше не надсилатимуться.\n\n"
        "Щоб підключити знову в групі використайте /setup, або в особистих розмовах з ботом.",
        parse_mode="Markdown",
    )
    await callback.answer()


# ==========================================
# === НОВЕ ЗРУЧНЕ МЕНЮ НАЛАШТУВАНЬ ===
# ==========================================


# --- 1. ГОЛОВНЕ МЕНЮ НАЛАШТУВАНЬ ---
async def show_settings_main(message: types.Message, user_id, edit=False):
    """Головна сторінка налаштувань."""
    user = await db.get_user(user_id)
    if not user:
        if edit:
            await message.edit_text("⚠️ Спочатку оберіть регіон через /start")
        else:
            await message.answer("⚠️ Спочатку оберіть регіон через /start")
        return

    settings = await db.get_user_settings(user_id)

    if settings["display_mode"] == "light":
        mode_status = "🟢 Показую, коли світло Є"
    else:
        mode_status = "⬛️ Показую, коли світла НЕМАЄ"

    # Відображаємо статус таймерів у меню (красиво)
    t_out = (
        f"{settings['notify_before']} хв"
        if settings["notify_before"] > 0
        else "Вимкнено"
    )
    t_in = (
        f"{settings['notify_return_before']} хв"
        if settings["notify_return_before"] > 0
        else "Вимкнено"
    )

    text = (
        f"⚙️ **Головні налаштування**\n"
        f"📍 Локація: **{user[0]}, Черга {user[1]}**\n\n"
        f"⏰ Таймер відключення: **{t_out}**\n"
        f"⏰ Таймер включення: **{t_in}**\n"
        f"🎨 Вигляд графіку: **{mode_status}**"
    )

    kb = InlineKeyboardBuilder()

    # Кнопки навігації (ієрархія)
    kb.button(text="⏰ Налаштувати таймери >", callback_data="menu_time_select")
    kb.button(text="🔔 Налаштування сповіщень >", callback_data="menu_types")
    kb.button(text="🎨 Вигляд графіку >", callback_data="menu_mode")
    kb.button(text="📍 Змінити область/чергу >", callback_data="open_regions")
    kb.button(text="📢 Мої канали/групи >", callback_data="menu_my_groups")
    kb.button(text="❌ Закрити меню", callback_data="close_settings")

    kb.adjust(1)

    if edit:
        await message.edit_text(
            text, reply_markup=kb.as_markup(), parse_mode="Markdown"
        )
    else:
        await message.answer(text, reply_markup=kb.as_markup(), parse_mode="Markdown")


# --- 2. ПІДМЕНЮ: ВИБІР ТАЙМЕРА (НОВЕ) ---
async def show_time_type_selection(message: types.Message):
    """Меню вибору: який таймер налаштовуємо?"""
    text = "⏰ **Налаштування часу**\n\nЯкий таймер ви хочете змінити?"

    kb = InlineKeyboardBuilder()
    kb.button(text="🔦 Відключення", callback_data="time_edit|outage")
    kb.button(text="💡 До включення", callback_data="time_edit|return")
    kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu_main"))

    await message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")


# --- 3. ПІДМЕНЮ: ВИБІР ХВИЛИН ---
async def show_minutes_menu(message: types.Message, user_id, timer_type):
    """Меню вибору хвилин для конкретного таймера."""
    settings = await db.get_user_settings(user_id)

    # Визначаємо, яку колонку редагуємо і який заголовок
    if timer_type == "outage":
        current = settings["notify_before"]
        title = "🔦 **Попередження про ВІДКЛЮЧЕННЯ**"
    else:
        current = settings["notify_return_before"]
        title = "💡 **Попередження про ВКЛЮЧЕННЯ**"

    text = f"{title}\n\n" f"За скільки хвилин вас попередити?"

    kb = InlineKeyboardBuilder()
    times = [5, 15, 30, 60]

    for t in times:
        mark = "✅" if current == t else ""
        label = "1 год" if t == 60 else f"{t} хв"
        # Передаємо тип таймера далі в callback
        kb.button(text=f"{mark} {label}", callback_data=f"set_time|{timer_type}|{t}")

    # Кнопка вимкнення (встановлює 0)
    mark_off = "✅" if current == 0 else ""
    kb.row(
        InlineKeyboardButton(
            text=f"{mark_off} 🔕 Не нагадувати",
            callback_data=f"set_time|{timer_type}|0",
        )
    )

    kb.adjust(2, 2, 1)
    kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu_time_select"))

    await message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")


# --- 4. ПІДМЕНЮ: ТИПИ СПОВІЩЕНЬ ---
async def show_types_menu(message: types.Message, user_id):
    settings = await db.get_user_settings(user_id)

    text = f"🔔 **Налаштування сповіщень**\n\n" f"Увімкніть або вимкніть повідомлення:"

    kb = InlineKeyboardBuilder()

    # 1. Відключення
    icon_out = "✅" if settings["notify_outage"] else "❌"
    kb.button(
        text=f"{icon_out} Коли зникає світло", callback_data="toggle|notify_outage"
    )

    # 2. Включення
    icon_ret = "✅" if settings["notify_return"] else "❌"
    kb.button(
        text=f"{icon_ret} Коли з'являється світло", callback_data="toggle|notify_return"
    )

    # 3. Зміни
    icon_chg = "✅" if settings["notify_changes"] else "❌"
    kb.button(
        text=f"{icon_chg} Якщо змінився графік", callback_data="toggle|notify_changes"
    )

    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu_main"))

    await message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")


# --- 5. ПІДМЕНЮ: ВИГЛЯД ГРАФІКУ ---
async def show_mode_menu(message: types.Message, user_id):
    settings = await db.get_user_settings(user_id)
    current = settings["display_mode"]

    text = f"🎨 **Вигляд графіку**\n\n" f"Що показувати на картинці?"

    kb = InlineKeyboardBuilder()

    mark_b = "✅" if current == "blackout" else ""
    kb.button(text=f"{mark_b} ⬛️ Коли світла НЕМАЄ", callback_data="set_mode|blackout")

    mark_l = "✅" if current == "light" else ""
    kb.button(text=f"{mark_l} 🟢 Коли світло Є", callback_data="set_mode|light")

    kb.adjust(1)
    kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu_main"))

    await message.edit_text(text, reply_markup=kb.as_markup(), parse_mode="Markdown")


# --- 6. ПІДМЕНЮ: МОЇ ГРУПИ/КАНАЛИ ---
async def show_my_groups_menu(message: types.Message, user_id):
    """Меню списку керованих груп/каналів."""
    groups = await db.get_user_managed_groups(user_id)

    kb = InlineKeyboardBuilder()

    if not groups:
        text = (
            "📢 **Мої канали/групи**\n\n"
            "Ви поки не додали жодного чату.\n\n"
            "💡 **Як додати?**\n"
            "1. Напишіть команду /addtogroup\n"
            "2. Додайте бота в чат\n"
            "3. Виконайте налаштування (команда /setup у групі)\n\n"
            "Після цього чат з'явиться у цьому списку."
        )
    else:
        text = (
            "📢 **Мої канали/групи**\n\n"
            "Виберіть чат для налаштування сповіщень:\n\n"
            "💡 _Щоб додати новий чат, використовуйте_ /addtogroup"
        )
        for grp in groups:
            chat_id, chat_title, chat_type, region, queue = grp
            icon = "📢" if chat_type == "channel" else "👥"
            display_title = (
                chat_title[:25] + "..." if len(chat_title) > 25 else chat_title
            )
            kb.button(
                text=f"{icon} {display_title}", callback_data=f"grp_menu_main|{chat_id}"
            )
        kb.adjust(1)

    kb.row(
        InlineKeyboardButton(text="🔄 Оновити список", callback_data="menu_my_groups")
    )
    kb.row(InlineKeyboardButton(text="🔙 Назад", callback_data="menu_main"))

    try:
        await message.edit_text(
            text, reply_markup=kb.as_markup(), parse_mode="Markdown"
        )
    except Exception as e:
        if "message is not modified" in str(e):
            # Ігноруємо якщо нічого не змінилося
            pass
        else:
            raise e


# --- ОБРОБНИКИ НАВІГАЦІЇ ТА ДІЙ ---


@router.callback_query(F.data == "menu_main")
async def nav_main(callback: types.CallbackQuery):
    await show_settings_main(callback.message, callback.from_user.id, edit=True)


@router.callback_query(F.data == "menu_time_select")
async def nav_time_select(callback: types.CallbackQuery):
    """Показує вибір типу таймера."""
    await show_time_type_selection(callback.message)


@router.callback_query(F.data.startswith("time_edit|"))
async def nav_time_edit(callback: types.CallbackQuery):
    """Показує вибір хвилин для конкретного типу."""
    timer_type = callback.data.split("|")[1]  # outage або return
    await show_minutes_menu(callback.message, callback.from_user.id, timer_type)


@router.callback_query(F.data == "menu_types")
async def nav_types(callback: types.CallbackQuery):
    await show_types_menu(callback.message, callback.from_user.id)


@router.callback_query(F.data == "menu_mode")
async def nav_mode(callback: types.CallbackQuery):
    await show_mode_menu(callback.message, callback.from_user.id)


@router.callback_query(F.data == "menu_my_groups")
async def nav_my_groups(callback: types.CallbackQuery):
    await show_my_groups_menu(callback.message, callback.from_user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("set_time|"))
async def set_notify_time(callback: types.CallbackQuery):
    """Встановлює час (універсальна функція)."""
    parts = callback.data.split("|")
    timer_type = parts[1]  # outage або return
    minutes = int(parts[2])

    # Визначаємо, в яку колонку писати
    col_name = "notify_before" if timer_type == "outage" else "notify_return_before"

    await db.update_user_setting(callback.from_user.id, col_name, minutes)

    # Оновлюємо це ж меню, щоб показати нову галочку
    await show_minutes_menu(callback.message, callback.from_user.id, timer_type)


@router.callback_query(F.data.startswith("toggle|"))
async def toggle_setting(callback: types.CallbackQuery):
    key = callback.data.split("|")[1]
    settings = await db.get_user_settings(callback.from_user.id)
    new_val = 0 if settings[key] else 1
    await db.update_user_setting(callback.from_user.id, key, new_val)
    await show_types_menu(callback.message, callback.from_user.id)


@router.callback_query(F.data.startswith("set_mode|"))
async def set_display_mode(callback: types.CallbackQuery):
    new_mode = callback.data.split("|")[1]
    await db.update_user_setting(callback.from_user.id, "display_mode", new_mode)
    await show_mode_menu(callback.message, callback.from_user.id)


@router.callback_query(F.data == "open_regions")
async def open_regions_handler(callback: types.CallbackQuery):
    await callback.message.delete()
    await show_regions_menu(callback.message, "👇 **Оберіть вашу область:**")


@router.callback_query(F.data == "close_settings")
async def close_settings_handler(callback: types.CallbackQuery):
    await callback.message.delete()


# ==========================================
# === ЛОГІКА ВИБОРУ РЕГІОНУ (СТАРА) ===
# ==========================================


async def show_regions_menu(message: types.Message, text):
    data = await api.fetch_api_data()
    if not data:
        await message.answer("⚠️ Помилка отримання даних.")
        return

    kb = InlineKeyboardBuilder()
    for region in data["regions"]:
        kb.button(text=region["name_ua"], callback_data=f"reg|{region['name_ua']}")
    kb.adjust(2)
    kb.row(
        InlineKeyboardButton(
            text="🔕 Зупинити бота (Відписатися)", callback_data="unsub"
        )
    )
    await message.answer(text, reply_markup=kb.as_markup(), parse_mode="Markdown")


@router.callback_query(F.data.startswith("reg|"))
async def select_region(callback: types.CallbackQuery):
    region_name = callback.data.split("|")[1]
    # Показуємо першу сторінку (page=0)
    await show_queue_page(callback, region_name, page=0)


async def show_queue_page(callback, region_name, page=0):
    """Показує сторінку черг для обраного регіону (пагінація по 12)."""
    QUEUES_PER_PAGE = 12

    data = await api.fetch_api_data()
    if not data:
        await callback.answer("⚠️ Помилка API", show_alert=True)
        return

    queues = []
    for r in data["regions"]:
        if r["name_ua"] == region_name:
            sch = r.get("schedule")
            if sch:
                queues = sorted(
                    sch.keys(),
                    key=lambda x: (
                        [int(p) for p in x.split(".")] if "." in x else [int(x), 0]
                    ),
                )
            break

    if not queues:
        await callback.answer("Черги не знайдено", show_alert=True)
        return

    total_pages = (len(queues) + QUEUES_PER_PAGE - 1) // QUEUES_PER_PAGE
    page = max(0, min(page, total_pages - 1))
    start = page * QUEUES_PER_PAGE
    page_queues = queues[start : start + QUEUES_PER_PAGE]

    kb = InlineKeyboardBuilder()
    for q in page_queues:
        kb.button(text=f"Черга {q}", callback_data=f"q|{region_name}|{q}")
    kb.adjust(3)

    # Навігація по сторінках (якщо більше 1 сторінки)
    if total_pages > 1:
        nav_buttons = []
        if page > 0:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="◀️ Назад", callback_data=f"qpage|{region_name}|{page-1}"
                )
            )
        nav_buttons.append(
            InlineKeyboardButton(
                text=f"📄 {page+1}/{total_pages}", callback_data="noop"
            )
        )
        if page < total_pages - 1:
            nav_buttons.append(
                InlineKeyboardButton(
                    text="Далі ▶️", callback_data=f"qpage|{region_name}|{page+1}"
                )
            )
        kb.row(*nav_buttons)

    # Кнопка повернення до списку областей
    kb.row(InlineKeyboardButton(text="🔙 До областей", callback_data="open_regions"))

    text = f"📍 **{region_name}**. Оберіть чергу:"
    if total_pages > 1:
        text += f"\n📄 Сторінка {page+1} з {total_pages} ({len(queues)} черг)"

    await callback.message.edit_text(
        text, reply_markup=kb.as_markup(), parse_mode="Markdown"
    )


@router.callback_query(F.data.startswith("qpage|"))
async def queue_page_nav(callback: types.CallbackQuery):
    """Навігація по сторінках черг."""
    _, region_name, page_str = callback.data.split("|")
    await show_queue_page(callback, region_name, page=int(page_str))


@router.callback_query(F.data == "noop")
async def noop_handler(callback: types.CallbackQuery):
    await callback.answer()


@router.callback_query(F.data.startswith("q|"))
async def select_queue(callback: types.CallbackQuery):
    _, region, queue = callback.data.split("|")
    await db.save_user(callback.from_user.id, region, queue)
    await callback.message.delete()
    await callback.message.answer(
        f"✅ Налаштування збережено!\n📍 {region}, Черга {queue}",
        reply_markup=get_main_keyboard(callback.from_user.id),
    )

    # 1. Показуємо графік
    await show_today_schedule(
        callback.message, region, queue, user_id=callback.from_user.id
    )

    # 2. НОВА ФІЧА: Відправляємо підказку про налаштування
    await asyncio.sleep(0.5)
    await callback.message.answer(
        "💡 **Маленька порада!**\n\n"
        "У меню **⚙️ Налаштування** ви можете:\n"
        "⏰ Змінити час сповіщення\n"
        "🎨 Вибрати «зелений» графік (коли світло є)\n"
        "🔔 Налаштувати повідомлення під себе",
        parse_mode="Markdown",
    )


async def show_today_schedule(
    message, region, queue, user_id=None, display_mode_override=None
):
    uid = user_id if user_id else message.from_user.id

    today = get_local_now().strftime("%Y-%m-%d")
    schedule = None

    # Якщо передано display_mode_override (з групи) — використовуємо його
    if display_mode_override:
        display_mode = display_mode_override
    else:
        settings = await db.get_user_settings(uid)
        display_mode = settings.get("display_mode", "blackout")

    cached_data = scheduler.schedules_cache.get((region, queue))

    if cached_data is not None:
        schedule = cached_data.get("today")
    else:
        data = await api.fetch_api_data()
        if data:
            for r in data["regions"]:
                if r["name_ua"] == region:
                    schedule = r["schedule"].get(queue, {}).get(today)
                    break

    if schedule:
        await db.save_stats(region, queue, today, api.calculate_off_hours(schedule))

    text = api.format_message(
        schedule, queue, today, is_tomorrow=False, display_mode=display_mode
    )

    # === ВИПРАВЛЕННЯ: Безпечна згадка імені ===
    if message.chat.type in ["group", "supergroup"]:
        # Якщо у юзера ім'я типу "User_Name" або "*Admin*", це ламає Markdown.
        # Тому ми вирізаємо небезпечні символи.
        if message.from_user:
            raw_name = message.from_user.first_name or "Користувач"
            safe_name = (
                raw_name.replace("*", "")
                .replace("_", "")
                .replace("`", "")
                .replace("[", "")
            )
            text = f"👤 **{safe_name}**, твій графік:\n" + text

    # Безпечна відправка
    try:
        await message.answer(text, parse_mode="Markdown")
    except Exception as e:
        print(f"Помилка Markdown: {e}")
        # Якщо помилка форматування - шлемо чистий текст без жирного шрифту
        clean_text = text.replace("**", "").replace("__", "").replace("`", "")
        await message.answer(clean_text)


# --- КНОПКИ МЕНЮ ---


@router.message(F.text == "⚙️ Налаштування")
async def btn_settings(message: types.Message):
    # ВІДКРИВАЄ НОВЕ ГОЛОВНЕ МЕНЮ
    await show_settings_main(message, message.from_user.id)


@router.message(F.text == "📅 Графік на сьогодні")
async def btn_today(message: types.Message):
    user = await db.get_user(message.from_user.id)
    if not user:
        return await message.answer("Спочатку зробіть налаштування.")
    await show_today_schedule(message, user[0], user[1], user_id=message.from_user.id)


@router.message(F.text == "🔮 Графік на завтра")
async def btn_tomorrow(message: types.Message):
    user = await db.get_user(message.from_user.id)
    if not user:
        return await message.answer("Спочатку налаштування.")

    settings = await db.get_user_settings(message.from_user.id)
    display_mode = settings.get("display_mode", "blackout")

    tomorrow = (get_local_now() + timedelta(days=1)).strftime("%Y-%m-%d")

    schedule = None

    cached_data = scheduler.schedules_cache.get((user[0], user[1]))

    if cached_data is not None:
        schedule = cached_data.get("tomorrow")
    else:
        data = await api.fetch_api_data()
        if data:
            for r in data["regions"]:
                if r["name_ua"] == user[0]:
                    schedule = r["schedule"].get(user[1], {}).get(tomorrow, None)
                    break

    if schedule:
        await db.save_stats(
            user[0], user[1], tomorrow, api.calculate_off_hours(schedule)
        )

    text = api.format_message(
        schedule, user[1], tomorrow, is_tomorrow=True, display_mode=display_mode
    )

    if message.chat.type in ["group", "supergroup"]:
        user_name = message.from_user.first_name
        text = f"👤 **{user_name}**, твій графік:\n" + text

    await message.answer(text, parse_mode="Markdown")


@router.message(F.text == "📊 Аналітика")
async def btn_stats(message: types.Message):
    user = await db.get_user(message.from_user.id)
    if not user:
        if message.chat.type in ["group", "supergroup"]:
            await message.answer("Налаштуйте бота в особистих повідомленнях.")
        return

    api_data = await api.fetch_api_data()

    total = 0
    lines = []

    current_date = get_local_now()

    for i in range(6, -1, -1):
        d = current_date - timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")

        val = await db.get_off_hours_for_date(user[0], user[1], d_str)
        if val is None and api_data:
            schedule = None
            for r in api_data["regions"]:
                if r["name_ua"] == user[0]:
                    schedule = r["schedule"].get(user[1], {}).get(d_str)
                    break
            if schedule:
                val = api.calculate_off_hours(schedule)
                await db.save_stats(user[0], user[1], d_str, val)
            else:
                val = 0
        elif val is None:
            val = 0

        total += val

        val_str = f"{int(val)}" if val == int(val) else f"{val:.1f}"
        d_nice = d.strftime("%d.%m")

        lines.append(f"▫️ {d_nice}:  **{val_str} год.**")

    total_str = f"{int(total)}" if total == int(total) else f"{total:.1f}"

    text = (
        f"📊 **Статистика відключень (останні 7 днів)**\n"
        f"📍 {user[0]}, Черга {user[1]}\n\n"
        + "\n".join(lines)
        + f"\n──────────────────\n"
        f"⚡️ Загалом: **{total_str} год.**"
    )
    await message.answer(text, parse_mode="Markdown")


@router.callback_query(F.data == "unsub")
async def unsub_handler(callback: types.CallbackQuery):
    await db.delete_user(callback.from_user.id)
    await callback.message.edit_text(
        "🔕 **Ви успішно відписалися.**", parse_mode="Markdown"
    )


# ========== СИСТЕМА ПІДТРИМКИ ==========


@router.message(F.text == "💬 Підтримка")
async def btn_support(message: types.Message):
    if message.chat.type in ["group", "supergroup"]:
        await message.answer("💬 Пишіть у підтримку в особисті повідомлення боту.")
        return

    await message.answer(
        "💬 **Служба підтримки**\n\n"
        "Напишіть ваше повідомлення, і адміністратор відповість вам найближчим часом.",
        parse_mode="Markdown",
    )
    await db.set_user_mode(message.from_user.id, "support")


@router.callback_query(F.data.startswith("user_reply|"))
async def user_reply_click(callback: types.CallbackQuery):
    ticket_id = callback.data.split("|")[1]

    ticket_info = await db.get_ticket_info(int(ticket_id))
    if not ticket_info:
        await callback.answer("❌ Помилка: тікет не знайдено", show_alert=True)
        return

    await db.set_user_mode(callback.from_user.id, f"user_replying:{ticket_id}")
    await callback.message.answer(
        "✍️ **Напишіть вашу відповідь:**", parse_mode="Markdown"
    )
    await callback.answer()


# ========== АДМІН-ПАНЕЛЬ ==========


@router.message(F.text == "👨‍💼 Адмін-панель")
@router.message(Command("admin"))
async def admin_menu(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    unread_count = await db.get_unread_count()

    kb = ReplyKeyboardBuilder()
    kb.row(KeyboardButton(text="📨 Розсилка всім"))

    support_text = f"📋 Підтримка"
    if unread_count > 0:
        support_text += f" ({unread_count})"

    kb.row(KeyboardButton(text=support_text), KeyboardButton(text="👥 Користувачів"))
    kb.row(KeyboardButton(text="🗑 Керування групами"))

    # === КНОПКИ КЕРУВАННЯ ===
    kb.row(
        KeyboardButton(text="⚙️ Керування джерелами"),
        KeyboardButton(text="🛰 API Статус"),
    )
    kb.row(KeyboardButton(text="🏠 Меню"))

    await message.answer(
        "👨‍💼 **Панель адміністратора**",
        reply_markup=kb.as_markup(resize_keyboard=True),
        parse_mode="Markdown",
    )


# === НОВЕ: КЕРУВАННЯ ДЖЕРЕЛАМИ ===
@router.message(F.text == "⚙️ Керування джерелами")
async def admin_sources_control(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    # Отримуємо поточний стан HOE сайту
    site_enabled = await db.get_system_config("hoe_site_enabled", "1")
    status_icon = "✅" if site_enabled == "1" else "❌"

    # Отримуємо стан API failover
    api_status = api.get_api_status()

    text = (
        "🛠 **Керування джерелами даних**\n\n"
        f"🛰 Активне API: {api_status['active_source_name']}\n"
        f"🔄 Перемикань: **{api_status['total_switches']}**\n"
        f"⏰ Останнє перемикання: **{api_status['last_switch']}**\n"
    )

    if api_status["emergency_regions"]:
        text += (
            f"⚠️ Екстрені регіони: **{', '.join(api_status['emergency_regions'])}**\n"
        )

    text += "\nНатисніть, щоб увімкнути/вимкнути:"

    kb = InlineKeyboardBuilder()
    kb.add(
        InlineKeyboardButton(
            text=f"🌐 Сайт HOE: {status_icon}", callback_data="toggle_hoe_site"
        )
    )

    # Кнопка примусового перемикання API
    current_api = api_status["active_source"]
    switch_text = (
        "➡️ Перемкнути на Резервне"
        if current_api == "primary"
        else "➡️ Повернути на Основне"
    )
    kb.add(InlineKeyboardButton(text=switch_text, callback_data="force_switch_api"))
    kb.adjust(1)

    await message.answer(text, reply_markup=kb.as_markup(), parse_mode="Markdown")


# === НОВЕ: КОМАНДА /api_status ===
@router.message(F.text == "🛰 API Статус")
@router.message(Command("api_status"))
async def api_status_command(message: types.Message):
    """API статус для адміна."""
    if message.from_user.id != ADMIN_ID:
        return

    status = api.get_api_status()

    text = (
        "🛰 **Статус API Failover**\n"
        "══════════════════\n"
        f"🎯 Активне: {status['active_source_name']}\n"
        f"🔄 Перемикань: **{status['total_switches']}**\n"
        f"⏰ Останнє перемикання: {status['last_switch']}\n"
        "\n**Основне API (DTEK Proxy):**\n"
        f"  • Невдач підряд: {status['primary_fails']}\n"
        f"  • Downtime: {status['primary_downtime']}\n"
        "\n**Резервне API:**\n"
        f"  • Невдач підряд: {status['backup_fails']}\n"
        f"⏳ Наступна перевірка recovery: {status['next_recovery_check']}\n"
    )

    if status["emergency_regions"]:
        text += f"\n🚨 **Екстренні регіони:** {', '.join(status['emergency_regions'])}"

    await message.answer(text, parse_mode="Markdown")


# === НОВЕ: ПРИМУСОВЕ ПЕРЕМИКАННЯ API ===
@router.callback_query(F.data == "force_switch_api")
async def force_switch_api_callback(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return

    current = api.api_state["active_source"]
    if current == "primary":
        api.api_state["active_source"] = "backup"
        api.api_state["last_primary_check"] = datetime.now()
        api.api_state["last_switch"] = datetime.now()
        api.api_state["total_switches"] += 1
        new_name = "🟡 Резервне API"
    else:
        api.api_state["active_source"] = "primary"
        api.api_state["primary_down_since"] = None
        api.api_state["backup_down_since"] = None
        api.api_state["last_switch"] = datetime.now()
        api.api_state["total_switches"] += 1
        new_name = "🟢 DTEK Proxy (Основне)"

    await call.answer(f"Активне: {new_name}", show_alert=True)

    # Оновлюємо повідомлення
    api_status_new = api.get_api_status()
    site_enabled = await db.get_system_config("hoe_site_enabled", "1")
    status_icon = "✅" if site_enabled == "1" else "❌"

    text = (
        "🛠 **Керування джерелами даних**\n\n"
        f"🛰 Активне API: {api_status_new['active_source_name']}\n"
        f"🔄 Перемикань: **{api_status_new['total_switches']}**\n"
        f"⏰ Останнє перемикання: **{api_status_new['last_switch']}**\n"
        "\nНатисніть, щоб увімкнути/вимкнути:"
    )

    kb = InlineKeyboardBuilder()
    kb.add(
        InlineKeyboardButton(
            text=f"🌐 Сайт HOE: {status_icon}", callback_data="toggle_hoe_site"
        )
    )
    new_api = api_status_new["active_source"]
    switch_text = (
        "➡️ Перемкнути на Резервне"
        if new_api == "primary"
        else "➡️ Повернути на Основне"
    )
    kb.add(InlineKeyboardButton(text=switch_text, callback_data="force_switch_api"))
    kb.adjust(1)

    await call.message.edit_text(
        text, reply_markup=kb.as_markup(), parse_mode="Markdown"
    )


@router.callback_query(F.data == "toggle_hoe_site")
async def toggle_hoe_site_callback(call: types.CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        return

    current = await db.get_system_config("hoe_site_enabled", "1")
    new_value = "0" if current == "1" else "1"
    await db.set_system_config("hoe_site_enabled", new_value)

    status_icon = "✅" if new_value == "1" else "❌"
    status_text = "ВІМКНЕНО" if new_value == "1" else "ВИМКНЕНО"

    # Оновлюємо повне повідомлення
    api_status_new = api.get_api_status()

    text = (
        "🛠 **Керування джерелами даних**\n\n"
        f"🛰 Активне API: {api_status_new['active_source_name']}\n"
        f"🔄 Перемикань: **{api_status_new['total_switches']}**\n"
        f"⏰ Останнє перемикання: **{api_status_new['last_switch']}**\n"
        "\nНатисніть, щоб увімкнути/вимкнути:"
    )

    kb = InlineKeyboardBuilder()
    kb.add(
        InlineKeyboardButton(
            text=f"🌐 Сайт HOE: {status_icon}", callback_data="toggle_hoe_site"
        )
    )
    current_api = api_status_new["active_source"]
    switch_text = (
        "➡️ Перемкнути на Резервне"
        if current_api == "primary"
        else "➡️ Повернути на Основне"
    )
    kb.add(InlineKeyboardButton(text=switch_text, callback_data="force_switch_api"))
    kb.adjust(1)

    await call.message.edit_text(
        text, reply_markup=kb.as_markup(), parse_mode="Markdown"
    )
    await call.answer(f"Парсинг сайту {status_text}")


@router.message(F.text == "📨 Розсилка всім")
async def broadcast_start(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "📨 **Розсилка всім**\nНапишіть текст повідомлення (максимум 4000 символів):"
    )
    await db.set_user_mode(ADMIN_ID, "broadcast")


@router.message(F.text.startswith("📋 Підтримка"))
async def support_tickets_menu(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    kb = InlineKeyboardBuilder()
    kb.button(text="🔔 Непрочитані", callback_data="tickets|unread")
    kb.button(text="📋 Всі звернення", callback_data="tickets|all")
    kb.adjust(2)

    unread_count = await db.get_unread_count()

    text = f"📋 **Служба підтримки**\n\n📌 Непрочитані: **{unread_count}**"
    await message.answer(text, reply_markup=kb.as_markup(), parse_mode="Markdown")


@router.callback_query(F.data.startswith("tickets|"))
async def show_tickets_list(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID and (not isinstance(ADMIN_IDS, list) or callback.from_user.id not in ADMIN_IDS):
        await callback.answer("У вас немає доступу.", show_alert=True)
        return
    await callback.answer()

    ticket_type = callback.data.split("|")[1]

    if ticket_type == "unread":
        tickets = await db.get_unread_tickets()
        title = "🔔 Непрочитані звернення"
    else:
        tickets = await db.get_all_tickets()
        title = "📋 Всі звернення"

    if not tickets:
        kb = InlineKeyboardBuilder()
        kb.button(text="◀️ Назад", callback_data="tickets|all" if ticket_type == "unread" else "tickets|unread")
        await callback.message.edit_text(
            f"{title}\n\n✅ Немає звернень", reply_markup=kb.as_markup(), parse_mode="Markdown"
        )
        return

    kb = InlineKeyboardBuilder()

    for ticket in tickets:
        ticket_id, user_id, username, *rest = ticket
        display_name = f"@{username}" if username else f"ID: {user_id}"

        if ticket_type == "all":
            status = rest[0]
            status_icon = (
                "🔴" if status == "unread" else "✅" if status == "read" else "🔒"
            )
            button_text = f"{status_icon} {display_name}"
        else:
            button_text = f"🔴 {display_name}"

        kb.button(text=button_text, callback_data=f"viewticket|{ticket_id}")

    kb.adjust(1)
    await callback.message.edit_text(f"{title}:", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("viewticket|"))
async def view_ticket(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID and (not isinstance(ADMIN_IDS, list) or callback.from_user.id not in ADMIN_IDS):
        await callback.answer("У вас немає доступу.", show_alert=True)
        return
    await callback.answer()

    ticket_id = int(callback.data.split("|")[1])

    ticket_info = await db.get_ticket_info(ticket_id)
    if not ticket_info:
        await callback.message.edit_text("❌ Тікет не знайдено")
        return

    user_id, username, status = ticket_info

    # Позначаємо як прочитаний
    if status == "unread":
        await db.mark_ticket_read(ticket_id)
        status = "read"

    safe_username = (username or 'Unknown').replace('_', '\\_').replace('*', '\\*').replace('`', '')

    messages = await db.get_ticket_messages(ticket_id)

    text = f"💬 **Звернення #{ticket_id}**\n"
    text += f"👤 @{safe_username} (ID: {user_id})\n"
    text += f"📊 Статус: {status}\n"
    text += "─────────────────\n\n"

    for from_user, msg_text, created_at in messages:
        icon = "👤" if from_user == "user" else "👨‍💼"
        safe_msg = msg_text.replace('_', '\\_').replace('*', '\\*').replace('`', '\\`')
        display_text = safe_msg[:200] + "..." if len(safe_msg) > 200 else safe_msg
        text += f"{icon} **{from_user}**: {display_text}\n⏰ {created_at}\n\n"

    kb = InlineKeyboardBuilder()
    kb.button(text="✍️ Відповісти", callback_data=f"reply|{ticket_id}")
    if status != "closed":
        kb.button(text="🔒 Закрити", callback_data=f"close|{ticket_id}")
    else:
        kb.button(text="🔓 Відкрити знову", callback_data=f"reopen|{ticket_id}")
    kb.button(text="🗑 Видалити", callback_data=f"delticket|{ticket_id}")
    kb.button(text="◀️ Назад", callback_data="tickets|all")
    kb.adjust(2, 2, 1)

    try:
        await callback.message.edit_text(
            text, reply_markup=kb.as_markup(), parse_mode="Markdown"
        )
    except Exception as e:
        print(f"[view_ticket markdown error]: {e}")
        clean_text = text.replace("**", "").replace("__", "").replace("`", "").replace("\\_", "_").replace("\\*", "*")
        await callback.message.edit_text(clean_text, reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("reply|"))
async def admin_reply_click(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    ticket_id = callback.data.split("|")[1]
    await db.set_user_mode(ADMIN_ID, f"replying:{ticket_id}")
    await callback.message.answer(
        f"✍️ **Введіть відповідь для тікету #{ticket_id}:**", parse_mode="Markdown"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("delticket|"))
async def delticket_handler(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return
    ticket_id = int(callback.data.split("|")[1])
    await db.delete_ticket(ticket_id)
    await callback.answer("✅ Тікет видалено!", show_alert=True)
    callback.data = "tickets|all"
    await show_tickets_list(callback)


@router.callback_query(F.data.startswith("close|"))
async def close_ticket_handler(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    ticket_id = int(callback.data.split("|")[1])
    await db.close_ticket(ticket_id)
    await callback.answer("✅ Тікет закрито", show_alert=True)
    await view_ticket(callback)


@router.callback_query(F.data.startswith("reopen|"))
async def reopen_ticket_handler(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    ticket_id = int(callback.data.split("|")[1])
    await db.reopen_ticket(ticket_id)
    await callback.answer("✅ Тікет відкрито знову", show_alert=True)
    await view_ticket(callback)


@router.message(F.text == "👥 Користувачів")
async def users_count(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    count = await db.get_users_count()
    active_count = await db.get_active_users_count()
    groups_count = await db.get_groups_count()

    text = f"👥 **Всього користувачів:** {count}\n"
    text += f"🟢 **Активні користувачі:** {active_count}\n"
    text += f"💬 **Підключених груп/каналів:** {groups_count}"

    if groups_count > 0:
        groups = await db.get_all_group_subs()
        text += "\n\n📋 **Список груп:**"
        for g in groups:
            chat_id, title, chat_type, region, queue = g
            type_icon = "📢" if chat_type == "channel" else "💬"
            text += f"\n{type_icon} {title or 'Без назви'} — {region}, Ч.{queue}"

    await message.answer(text, parse_mode="Markdown")


@router.message(F.text == "🗑 Керування групами")
async def admin_manage_groups(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return

    groups = await db.get_all_group_subs()
    if not groups:
        await message.answer("💬 Немає підключених груп.")
        return

    kb = InlineKeyboardBuilder()
    for g in groups:
        chat_id, title, chat_type, region, queue = g
        type_icon = "📢" if chat_type == "channel" else "💬"
        display_name = f"{type_icon} {title or 'Без назви'} ({region}, Ч.{queue})"
        kb.button(text=f"❌ {display_name[:30]}", callback_data=f"adm_delgrp|{chat_id}")

    kb.adjust(1)
    await message.answer(
        "🗑 **Оберіть групу для видалення:**",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown",
    )


@router.callback_query(F.data.startswith("adm_delgrp|"))
async def admin_delete_group_callback(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        return

    chat_id = int(callback.data.split("|")[1])
    await db.delete_group_sub(chat_id)

    # Оновлюємо список
    groups = await db.get_all_group_subs()
    if not groups:
        await callback.message.edit_text("💬 Немає підключених груп.")
        await callback.answer("✅ Групу видалено", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    for g in groups:
        c_id, title, chat_type, region, queue = g
        type_icon = "📢" if chat_type == "channel" else "💬"
        display_name = f"{type_icon} {title or 'Без назви'} ({region}, Ч.{queue})"
        kb.button(text=f"❌ {display_name[:30]}", callback_data=f"adm_delgrp|{c_id}")

    kb.adjust(1)
    await callback.message.edit_text(
        "🗑 **Оберіть групу для видалення:**",
        reply_markup=kb.as_markup(),
        parse_mode="Markdown",
    )
    await callback.answer("✅ Групу видалено", show_alert=True)


@router.message(F.text == "🏠 Меню")
async def back_to_main(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(
        "🏠 **Головне меню.**",
        reply_markup=get_main_keyboard(ADMIN_ID),
        parse_mode="Markdown",
    )
    await db.set_user_mode(ADMIN_ID, "normal")


# ========== ОБРОБКА ТЕКСТОВИХ ПОВІДОМЛЕНЬ ==========


@router.message(F.text)
async def handle_text_messages(message: types.Message):
    user_id = message.from_user.id
    mode = await db.get_user_mode(user_id)

    # 1. АДМІН: РОЗСИЛКА
    if user_id == ADMIN_ID and mode == "broadcast":
        if len(message.text) > 4000:
            await message.answer(
                "❌ **Повідомлення занадто довге!**", parse_mode="Markdown"
            )
            return

        users = await db.get_all_users_for_broadcast()
        sent, failed = 0, 0
        if users:
            await message.answer(f"📤 Відправка {len(users)} користувачам...")
            for (uid,) in users:
                try:
                    await message.bot.send_message(
                        uid,
                        f"📢 **Сповіщення:**\n\n{message.text}",
                        parse_mode="Markdown",
                    )
                    sent += 1
                    await asyncio.sleep(0.05)
                except (TelegramForbiddenError, TelegramBadRequest):
                    failed += 1
                    await db.mark_user_inactive(uid)
                except Exception:
                    failed += 1

            await message.answer(
                f"✅ **Розсилка завершена!**\n✓ {sent} / ✗ {failed}",
                parse_mode="Markdown",
            )
        else:
            await message.answer("❌ Немає користувачів.")

        await db.set_user_mode(ADMIN_ID, "normal")
        await message.answer(
            "🏠 Головне меню", reply_markup=get_main_keyboard(ADMIN_ID)
        )
        return

    # 2. АДМІН: ВІДПОВІДЬ НА ТІКЕТ
    if user_id == ADMIN_ID and mode.startswith("replying:"):
        ticket_id = int(mode.split(":")[1])

        if len(message.text) > 3000:
            await message.answer(
                "❌ **Повідомлення занадто довге!**", parse_mode="Markdown"
            )
            return

        ticket_info = await db.get_ticket_info(ticket_id)
        if not ticket_info:
            await message.answer("❌ Тікет не знайдено")
            await db.set_user_mode(ADMIN_ID, "normal")
            return

        target_user_id, username, status = ticket_info

        await db.save_support_message(ticket_id, "admin", message.text)
        await db.mark_ticket_read(ticket_id)

        try:
            kb = InlineKeyboardBuilder()
            kb.button(text="✍️ Відповісти", callback_data=f"user_reply|{ticket_id}")

            await message.bot.send_message(
                target_user_id,
                f"📞 **Служба підтримки:**\n\n{message.text}",
                reply_markup=kb.as_markup(),
                parse_mode="Markdown",
            )
            await message.answer("✅ Відповідь надіслано!")
        except Exception as e:
            await message.answer(f"❌ Не вдалося надіслати: {e}")

        await db.set_user_mode(ADMIN_ID, "normal")
        await message.answer(
            "🏠 Головне меню", reply_markup=get_main_keyboard(ADMIN_ID)
        )
        return

    # 3. КОРИСТУВАЧ: ПІДТРИМКА
    if mode == "support":
        if message.chat.type in ["group", "supergroup"]:
            return

        if len(message.text) > 3000:
            await message.answer(
                "❌ **Повідомлення занадто довге!**", parse_mode="Markdown"
            )
            return

        username = message.from_user.username or "Unknown"
        ticket_id = await db.create_or_get_ticket(user_id, username)

        await db.save_support_message(ticket_id, "user", message.text)

        try:
            kb = InlineKeyboardBuilder()
            kb.button(text="✍️ Відповісти", callback_data=f"reply|{ticket_id}")
            kb.button(text="📋 Переглянути", callback_data=f"viewticket|{ticket_id}")

            display_text = (
                message.text[:500] + "..." if len(message.text) > 500 else message.text
            )

            # === ФІКС: ПРИБРАНО parse_mode ДЛЯ АДМІНА ===
            await message.bot.send_message(
                ADMIN_ID,
                f"🔔 Нове повідомлення в тікеті #{ticket_id}\n"
                f"👤 @{username} (ID: {user_id})\n\n"
                f"💬 {display_text}",
                reply_markup=kb.as_markup(),
            )
            await message.answer(
                "✅ Повідомлення відправлено! Адміністратор відповість найближчим часом."
            )
        except Exception as e:
            print(f"Помилка відправки адміну: {e}")
            await message.answer("✅ Повідомлення збережено!")

        await db.set_user_mode(user_id, "normal")
        await message.answer("🏠 Головне меню", reply_markup=get_main_keyboard(user_id))
        return

    # 4. КОРИСТУВАЧ: ВІДПОВІДЬ
    if mode.startswith("user_replying:"):
        ticket_id = int(mode.split(":")[1])
        username = message.from_user.username or "Unknown"

        if len(message.text) > 3000:
            await message.answer("❌ **Занадто довге!**", parse_mode="Markdown")
            return

        await db.save_support_message(ticket_id, "user", message.text)
        await db.reopen_ticket(ticket_id)

        try:
            kb = InlineKeyboardBuilder()
            kb.button(text="✍️ Відповісти", callback_data=f"reply|{ticket_id}")
            kb.button(text="📋 Переглянути", callback_data=f"viewticket|{ticket_id}")

            display_text = (
                message.text[:500] + "..." if len(message.text) > 500 else message.text
            )

            # === ФІКС: ПРИБРАНО parse_mode ДЛЯ АДМІНА ===
            await message.bot.send_message(
                ADMIN_ID,
                f"🔔 Нова відповідь в тікеті #{ticket_id}\n"
                f"👤 @{username} (ID: {user_id})\n\n"
                f"💬 {display_text}",
                reply_markup=kb.as_markup(),
            )
            await message.answer("✅ Відповідь відправлена!")
        except Exception as e:
            print(f"Помилка: {e}")
            await message.answer("✅ Відповідь збережена!")

        await db.set_user_mode(user_id, "normal")
        await message.answer("🏠 Головне меню", reply_markup=get_main_keyboard(user_id))
        return

    await message.answer("❓ Не розумію вашу команду. Використовуйте кнопки меню.")
