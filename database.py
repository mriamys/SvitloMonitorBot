# database.py - MySQL backend for SvitloMonitorBot
import os
import re
import aiomysql
from datetime import datetime, timedelta

DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_USER = os.getenv("DB_USER", "svitlo")
DB_PASS = os.getenv("DB_PASS", "svitlo_secure_pass_2026")
DB_NAME = os.getenv("DB_NAME", "svitlo_bot")
DB_PORT = int(os.getenv("DB_PORT", "3306"))

_pool = None

async def get_pool():
    global _pool
    if _pool is None:
        _pool = await aiomysql.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASS,
            db=DB_NAME,
            charset='utf8mb4',
            # autocommit=True обов'язково: інакше пул-з'єднання тримають старий
            # snapshot (REPEATABLE READ) і бачать застарілі дані (режим підтримки,
            # таймери тощо не оновлювалися).
            autocommit=True,
            minsize=2,
            maxsize=20
        )
    return _pool

class MySQLCompatCursor:
    def __init__(self, cur):
        self.cur = cur

    async def fetchone(self):
        return await self.cur.fetchone()

    async def fetchall(self):
        return await self.cur.fetchall()

    @property
    def rowcount(self):
        return self.cur.rowcount

    @property
    def lastrowid(self):
        return self.cur.lastrowid

class MySQLCompatConnection:
    def __init__(self, pool):
        self.pool = pool
        self.conn = None

    async def __aenter__(self):
        self.conn = await self.pool.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            self.pool.release(self.conn)

    def execute(self, query, params=None):
        return MySQLCursorContext(self.conn, query, params)

    async def commit(self):
        if self.conn:
            await self.conn.commit()

class MySQLCursorContext:
    def __init__(self, conn, query, params):
        self.conn = conn
        q = query
        q = re.sub(r'ON\s+CONFLICT\s*\([^)]*\)\s*DO\s+UPDATE\s+SET', 'ON DUPLICATE KEY UPDATE', q, flags=re.IGNORECASE)
        q = re.sub(r'excluded\.(\w+)', r'VALUES(\1)', q, flags=re.IGNORECASE)
        q = re.sub(r'INSERT\s+OR\s+IGNORE', 'INSERT IGNORE', q, flags=re.IGNORECASE)
        q = re.sub(r'(?<![`\w])key(?!\s*=\s*VALUES)(?![`\w])', '`key`', q, flags=re.IGNORECASE)
        q = re.sub(r'ON\s+DUPLICATE\s+`key`\s+UPDATE', 'ON DUPLICATE KEY UPDATE', q, flags=re.IGNORECASE)
        q = re.sub(r'(?<![`\w])date(?![`\w])', '`date`', q, flags=re.IGNORECASE)
        q = q.replace('?', '%s')
        self.query = q
        self.params = params
        self.cur = None

    async def _execute(self):
        self.cur = await self.conn.cursor()
        try:
            await self.cur.execute(self.query, self.params)
        except Exception as e:
            if "Duplicate column name" in str(e) or "already exists" in str(e):
                pass
            else:
                print(f"[SQL Error]: {e} on query: {self.query}")
                raise e
        return MySQLCompatCursor(self.cur)

    def __await__(self):
        return self._execute().__await__()

    async def __aenter__(self):
        return await self._execute()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.cur:
            await self.cur.close()

class DBConnectContext:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        p = await get_pool()
        self.compat = MySQLCompatConnection(p)
        return await self.compat.__aenter__()

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.compat.__aexit__(exc_type, exc_val, exc_tb)

def connect_db(*args, **kwargs):
    return DBConnectContext(*args, **kwargs)

# database.py

from datetime import datetime, timedelta



async def init_db():
    """Таблиці створені в MySQL."""
    return


async def save_user(user_id, region, queue):
    """Зберігає або оновлює вибір користувача."""
    async with connect_db() as db:
        await db.execute(
            """
            INSERT INTO users (user_id, region, queue) 
            VALUES (?, ?, ?) 
            ON CONFLICT(user_id) DO UPDATE SET region=excluded.region, queue=excluded.queue, is_active=1
        """,
            (user_id, region, queue),
        )
        await db.commit()


async def get_user(user_id):
    """Повертає регіон і чергу користувача."""
    async with connect_db() as db:
        async with db.execute(
            "SELECT region, queue FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            return await cur.fetchone()


# --- НОВІ ФУНКЦІЇ ДЛЯ НАЛАШТУВАНЬ ---


async def get_user_settings(user_id):
    """Отримує всі налаштування користувача."""
    async with connect_db() as db:
        # Якщо користувача немає або поля пусті, повертаємо дефолтні налаштування
        defaults = {
            "notify_before": 5,
            "notify_return_before": 0,  # СТАНДАРТ: 0 (Вимкнено)
            "notify_outage": 1,
            "notify_return": 1,
            "notify_changes": 1,
            "display_mode": "blackout",
        }

        # Пробуємо отримати нові поля.
        try:
            # Додали notify_return_before у запит
            async with db.execute(
                """
                SELECT notify_before, notify_outage, notify_return, notify_changes, display_mode, notify_return_before 
                FROM users WHERE user_id = ?
            """,
                (user_id,),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return {
                        "notify_before": row[0] if row[0] is not None else 5,
                        "notify_outage": row[1] if row[1] is not None else 1,
                        "notify_return": row[2] if row[2] is not None else 1,
                        "notify_changes": row[3] if row[3] is not None else 1,
                        "display_mode": row[4] if row[4] is not None else "blackout",
                        # Беремо значення з бази або 0, якщо його ще немає
                        "notify_return_before": (
                            row[5] if (len(row) > 5 and row[5] is not None) else 0
                        ),
                    }
        except Exception as e:
            # print(f"Error getting settings: {e}")
            pass

        return defaults


async def update_user_setting(user_id, key, value):
    """Оновлює конкретне налаштування."""
    # Додали notify_return_before у список дозволених ключів
    allowed_keys = [
        "notify_before",
        "notify_return_before",
        "notify_outage",
        "notify_return",
        "notify_changes",
        "display_mode",
    ]
    if key not in allowed_keys:
        return

    async with connect_db() as db:
        await db.execute(
            f"UPDATE users SET {key} = ? WHERE user_id = ?", (value, user_id)
        )
        await db.commit()


# --- КІНЕЦЬ НОВИХ ФУНКЦІЙ ---


async def save_stats(region, queue, date_str, off_hours):
    """Записує статистику за день."""
    async with connect_db() as db:
        await db.execute(
            """
            INSERT INTO daily_stats (date, region, queue, off_hours) 
            VALUES (?, ?, ?, ?) 
            ON CONFLICT(date, region, queue) DO UPDATE SET off_hours=excluded.off_hours
        """,
            (date_str, region, queue, off_hours),
        )
        await db.commit()


async def get_stats_data(region, queue):
    """Отримує статистику за останні 7 днів (від сьогодні і назад)."""
    async with connect_db() as db:
        sql = """
            SELECT date, off_hours 
            FROM daily_stats 
            WHERE region = ? AND queue = ? 
            ORDER BY date DESC 
            LIMIT 7
        """
        async with db.execute(sql, (region, queue)) as cur:
            rows = await cur.fetchall()
            return list(reversed(rows))


async def get_all_subs():
    """Отримує список всіх унікальних підписок (регіон + черга)."""
    async with connect_db() as db:
        async with db.execute("SELECT DISTINCT region, queue FROM users") as cur:
            return await cur.fetchall()


async def get_users_by_queue(region, queue):
    """Отримує ID всіх користувачів конкретної черги."""
    async with connect_db() as db:
        async with db.execute(
            "SELECT user_id FROM users WHERE region = ? AND queue = ?", (region, queue)
        ) as cur:
            return await cur.fetchall()


async def delete_user(user_id):
    """Видаляє користувача з бази даних (відписка)."""
    async with connect_db() as db:
        await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        await db.commit()


async def set_user_mode(user_id, mode):
    """Встановлює режим користувача (normal, support, admin)."""
    async with connect_db() as db:
        await db.execute("UPDATE users SET mode = ? WHERE user_id = ?", (mode, user_id))
        await db.commit()


async def get_user_mode(user_id):
    """Отримує режим користувача."""
    async with connect_db() as db:
        async with db.execute(
            "SELECT mode FROM users WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else "normal"


async def get_users_count():
    """Отримує кількість всіх користувачів."""
    async with connect_db() as db:
        async with db.execute("SELECT COUNT(*) FROM users") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def get_active_users_count():
    """Отримує кількість активних користувачів."""
    async with connect_db() as db:
        # Враховуємо і 1, і NULL (на випадок якщо ALTER TABLE не застосував дефолт)
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE is_active = 1 OR is_active IS NULL"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def mark_user_active(user_id):
    """Позначає користувача активним."""
    async with connect_db() as db:
        await db.execute("UPDATE users SET is_active = 1 WHERE user_id = ?", (user_id,))
        await db.commit()


async def mark_user_inactive(user_id):
    """Позначає користувача неактивним (заблокував бота)."""
    async with connect_db() as db:
        await db.execute("UPDATE users SET is_active = 0 WHERE user_id = ?", (user_id,))
        await db.commit()


async def get_all_users_for_broadcast():
    """Отримує всіх користувачів для розсилки."""
    async with connect_db() as db:
        async with db.execute("SELECT DISTINCT user_id FROM users") as cur:
            return await cur.fetchall()


async def cleanup_old_stats():
    """Видаляє статистику старше 7 днів."""
    async with connect_db() as db:
        cutoff_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        await db.execute("DELETE FROM daily_stats WHERE date < ?", (cutoff_date,))
        await db.commit()


async def get_off_hours_for_date(region, queue, date_str):
    """Отримує години відключення для конкретної дати."""
    async with connect_db() as db:
        async with db.execute(
            "SELECT off_hours FROM daily_stats WHERE region = ? AND queue = ? AND date = ?",
            (region, queue, date_str),
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


# === НОВІ ФУНКЦІЇ ДЛЯ КОНФІГУРАЦІЇ ===


async def set_system_config(key, value):
    """Зберігає системне налаштування."""
    async with connect_db() as db:
        await db.execute(
            "INSERT INTO system_config (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        await db.commit()


async def get_system_config(key, default=None):
    """Отримує системне налаштування."""
    async with connect_db() as db:
        async with db.execute(
            "SELECT value FROM system_config WHERE key = ?", (key,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else default


# ========== НОВА СИСТЕМА ПІДТРИМКИ ==========


async def create_or_get_ticket(user_id, username):
    """Створює новий тікет або повертає існуючий відкритий тікет."""
    async with connect_db() as db:
        # Перевіряємо чи є відкритий тікет
        async with db.execute(
            "SELECT ticket_id FROM support_tickets WHERE user_id = ? AND status != 'closed' ORDER BY last_message_at DESC LIMIT 1",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            if row:
                return row[0]

        # Створюємо новий тікет
        async with db.execute(
            "INSERT INTO support_tickets (user_id, username, status) VALUES (?, ?, 'unread')",
            (user_id, username),
        ) as cur:
            await db.commit()
            return cur.lastrowid


async def save_support_message(ticket_id, from_user, message_text):
    """Зберігає повідомлення в тікет."""
    async with connect_db() as db:
        await db.execute(
            "INSERT INTO support_messages (ticket_id, from_user, message_text) VALUES (?, ?, ?)",
            (ticket_id, from_user, message_text),
        )
        # Оновлюємо час останнього повідомлення
        await db.execute(
            "UPDATE support_tickets SET last_message_at = CURRENT_TIMESTAMP WHERE ticket_id = ?",
            (ticket_id,),
        )
        await db.commit()


async def get_unread_tickets():
    """Отримує всі непрочитані тікети."""
    async with connect_db() as db:
        async with db.execute("""
            SELECT t.ticket_id, t.user_id, t.username, t.created_at, t.last_message_at,
                   (SELECT COUNT(*) FROM support_messages WHERE ticket_id = t.ticket_id) as msg_count
            FROM support_tickets t
            WHERE t.status = 'unread'
            ORDER BY t.last_message_at DESC
        """) as cur:
            return await cur.fetchall()


async def get_all_tickets():
    """Отримує всі тікети."""
    async with connect_db() as db:
        async with db.execute("""
            SELECT t.ticket_id, t.user_id, t.username, t.status, t.created_at, t.last_message_at,
                   (SELECT COUNT(*) FROM support_messages WHERE ticket_id = t.ticket_id) as msg_count
            FROM support_tickets t
            ORDER BY t.last_message_at DESC
            LIMIT 20
        """) as cur:
            return await cur.fetchall()


async def get_ticket_messages(ticket_id):
    """Отримує всі повідомлення тікету."""
    async with connect_db() as db:
        async with db.execute(
            """
            SELECT from_user, message_text, created_at
            FROM support_messages
            WHERE ticket_id = ?
            ORDER BY created_at ASC
        """,
            (ticket_id,),
        ) as cur:
            return await cur.fetchall()


async def mark_ticket_read(ticket_id):
    """Позначає тікет як прочитаний."""
    async with connect_db() as db:
        await db.execute(
            "UPDATE support_tickets SET status = 'read' WHERE ticket_id = ?",
            (ticket_id,),
        )
        await db.commit()


async def close_ticket(ticket_id):
    """Закриває тікет."""
    async with connect_db() as db:
        await db.execute(
            "UPDATE support_tickets SET status = 'closed' WHERE ticket_id = ?",
            (ticket_id,),
        )
        await db.commit()


async def reopen_ticket(ticket_id):
    """Знову відкриває тікет."""
    async with connect_db() as db:
        await db.execute(
            "UPDATE support_tickets SET status = 'unread', last_message_at = CURRENT_TIMESTAMP WHERE ticket_id = ?",
            (ticket_id,),
        )
        await db.commit()


async def get_ticket_info(ticket_id):
    """Отримує інформацію про тікет."""
    async with connect_db() as db:
        async with db.execute(
            "SELECT user_id, username, status FROM support_tickets WHERE ticket_id = ?",
            (ticket_id,),
        ) as cur:
            return await cur.fetchone()


async def get_unread_count():
    """Отримує кількість непрочитаних тікетів."""
    async with connect_db() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM support_tickets WHERE status = 'unread'"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


# ========== ФУНКЦІЇ ДЛЯ ГРУП І КАНАЛІВ ==========


async def save_group_sub(chat_id, chat_title, chat_type, region, queue, added_by):
    """Зберігає або оновлює підписку групи/каналу."""
    async with connect_db() as db:
        await db.execute(
            """
            INSERT INTO group_subscriptions (chat_id, chat_title, chat_type, region, queue, added_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET 
                chat_title=excluded.chat_title, 
                region=excluded.region, 
                queue=excluded.queue,
                added_by=excluded.added_by
        """,
            (chat_id, chat_title, chat_type, region, queue, added_by),
        )
        await db.commit()


async def get_group_sub(chat_id):
    """Повертає підписку групи/каналу."""
    async with connect_db() as db:
        async with db.execute(
            "SELECT region, queue, display_mode, notify_outage, notify_return, notify_changes, notify_morning FROM group_subscriptions WHERE chat_id = ?",
            (chat_id,),
        ) as cur:
            return await cur.fetchone()


async def delete_group_sub(chat_id):
    """Видаляє підписку групи/каналу."""
    async with connect_db() as db:
        await db.execute(
            "DELETE FROM group_subscriptions WHERE chat_id = ?", (chat_id,)
        )
        await db.commit()


async def get_all_group_subs():
    """Отримує список усіх підписок груп/каналів."""
    async with connect_db() as db:
        async with db.execute(
            "SELECT chat_id, chat_title, chat_type, region, queue FROM group_subscriptions"
        ) as cur:
            return await cur.fetchall()


async def get_user_managed_groups(user_id):
    """Отримує всі групи/канали, додані цим користувачем."""
    async with connect_db() as db:
        async with db.execute(
            "SELECT chat_id, chat_title, chat_type, region, queue FROM group_subscriptions WHERE added_by = ?",
            (user_id,),
        ) as cur:
            return await cur.fetchall()


async def get_groups_by_queue(region, queue):
    """Отримує групи/канали з конкретною чергою (для розсилки)."""
    async with connect_db() as db:
        async with db.execute(
            """
            SELECT chat_id, display_mode, notify_outage, notify_return, notify_changes, notify_morning, notify_before, notify_return_before 
            FROM group_subscriptions 
            WHERE region = ? AND queue = ?
        """,
            (region, queue),
        ) as cur:
            return await cur.fetchall()


async def update_group_setting(chat_id, key, value):
    """Оновлює конкретне налаштування групи."""
    allowed_keys = [
        "display_mode",
        "notify_outage",
        "notify_return",
        "notify_changes",
        "notify_morning",
        "notify_before",
        "notify_return_before",
    ]
    if key not in allowed_keys:
        return
    async with connect_db() as db:
        await db.execute(
            f"UPDATE group_subscriptions SET {key} = ? WHERE chat_id = ?",
            (value, chat_id),
        )
        await db.commit()


async def get_group_settings(chat_id):
    """Отримує налаштування групи."""
    defaults = {
        "display_mode": "blackout",
        "notify_outage": 1,
        "notify_return": 1,
        "notify_changes": 1,
        "notify_morning": 1,
        "notify_before": 5,
        "notify_return_before": 0,
    }
    async with connect_db() as db:
        try:
            async with db.execute(
                """
                SELECT display_mode, notify_outage, notify_return, notify_changes, notify_morning, notify_before, notify_return_before
                FROM group_subscriptions WHERE chat_id = ?
            """,
                (chat_id,),
            ) as cur:
                row = await cur.fetchone()
                if row:
                    return {
                        "display_mode": row[0] or "blackout",
                        "notify_outage": row[1] if row[1] is not None else 1,
                        "notify_return": row[2] if row[2] is not None else 1,
                        "notify_changes": row[3] if row[3] is not None else 1,
                        "notify_morning": row[4] if row[4] is not None else 1,
                        "notify_before": (
                            row[5] if len(row) > 5 and row[5] is not None else 5
                        ),
                        "notify_return_before": (
                            row[6] if len(row) > 6 and row[6] is not None else 0
                        ),
                    }
        except Exception:
            pass
    return defaults


async def get_groups_count():
    """Отримує кількість підключених груп/каналів."""
    async with connect_db() as db:
        async with db.execute("SELECT COUNT(*) FROM group_subscriptions") as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def delete_ticket(ticket_id):
    """Повністю видаляє тікет та його повідомлення."""
    async with connect_db() as db:
        await db.execute("DELETE FROM support_messages WHERE ticket_id = ?", (ticket_id,))
        await db.execute("DELETE FROM support_tickets WHERE ticket_id = ?", (ticket_id,))
        await db.commit()
