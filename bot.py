import os
from pathlib import Path
from dotenv import load_dotenv
import asyncio
import html
import hashlib
import logging
import random
import sqlite3
import threading
import time
from contextlib import closing
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.error import (
    TelegramError,
    RetryAfter,
    TimedOut,
    NetworkError,
    BadRequest,
    Forbidden,
)
from telegram.ext import (
    Application,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    CommandHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    filters,
)
from telegram.request import HTTPXRequest


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROUP_CHAT_ID = int(os.getenv("GROUP_CHAT_ID", "-1003908738339").strip())
ADMIN_ID = int(os.getenv("ADMIN_ID", "403627901").strip())

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing. Add BOT_TOKEN=your_token to the .env file "
        "in the same folder as bot.py."
    )


# ============================================================
# WORKING HOURS (fixed UTC+03:00)
# ============================================================

UTC_PLUS_3 = timezone(timedelta(hours=3))

SCHEDULE_START_HOUR = 8
SCHEDULE_START_MINUTE = 30
SCHEDULE_END_HOUR = 17
SCHEDULE_END_MINUTE = 0

FRIDAY_LUNCH_START = 11 * 60 + 30
FRIDAY_LUNCH_END = 13 * 60 + 30

DISPLAY_START = "8:30 AM"
DISPLAY_END = "5:00 PM"
DISPLAY_LUNCH = "11:30 AM – 1:30 PM"


# ============================================================
# DATABASE / RELIABILITY SETTINGS
# ============================================================

DATABASE_FILE = os.getenv("DATABASE_FILE", "dars_bot.db")

MAX_CONCURRENT_USERS = 100
CONNECTION_POOL_SIZE = 100
GET_UPDATES_POOL_SIZE = 10
TELEGRAM_API_CONCURRENCY = 20

CONNECT_TIMEOUT = 60
READ_TIMEOUT = 90
WRITE_TIMEOUT = 90
POOL_TIMEOUT = 90
MEDIA_WRITE_TIMEOUT = 90

MAX_RETRIES = 8
INITIAL_RETRY_DELAY = 2
MAX_RETRY_DELAY = 45

DATABASE_MAX_RETRIES = 8
DATABASE_INITIAL_RETRY_DELAY = 0.25
DATABASE_MAX_RETRY_DELAY = 5


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger("DARS_LOCATION_BOT")


# ============================================================
# TIME / SCHEDULE
# ============================================================

def get_utc_plus_3_now():
    return datetime.now(UTC_PLUS_3)


def is_admin_user(user_id):
    return user_id is not None and int(user_id) == ADMIN_ID


def evaluate_schedule(weekday, hour, minute):
    current = hour * 60 + minute
    start = SCHEDULE_START_HOUR * 60 + SCHEDULE_START_MINUTE
    end = SCHEDULE_END_HOUR * 60 + SCHEDULE_END_MINUTE

    if weekday == 6:
        return "sunday"

    if current < start:
        return "before"

    if current >= end:
        return "after"

    if weekday == 4 and FRIDAY_LUNCH_START <= current < FRIDAY_LUNCH_END:
        return "friday_lunch"

    return "working"


def get_real_schedule_status():
    now = get_utc_plus_3_now()
    reason = evaluate_schedule(now.weekday(), now.hour, now.minute)

    return {
        "working": reason == "working",
        "reason": reason,
        "weekday": now.weekday(),
        "now": now,
        "start": DISPLAY_START,
        "end": DISPLAY_END,
    }


def get_schedule_status():
    return get_real_schedule_status()


def get_working_hours_text():
    return (
        "🕐 <b>የሥራ ሰዓት</b>\n\n"
        f"ሰኞ – ዓርብ፦ <b>{DISPLAY_START} – {DISPLAY_END}</b>\n"
        f"ቅዳሜ፦ <b>{DISPLAY_START} – {DISPLAY_END}</b>\n"
        "እሁድ፦ <b>ዝግ</b>\n\n"
        "⚠️ ከሥራ ሰዓት ውጭ መደበኛ ተጠቃሚዎች "
        "ቦቱን መጠቀም አይችሉም።\n"
        "<b>Owned by @BEREKT1617</b>"
    )


NEXT_DAY_NAMES = {
    0: "ማክሰኞ",
    1: "ረቡዕ",
    2: "ሐሙስ",
    3: "ዓርብ",
    4: "ቅዳሜ",
}


def get_next_working_period(status):
    weekday = status.get("weekday", 0)
    reason = status.get("reason", "")

    if reason == "before":
        return (
            "ዛሬ የሥራ ሰዓቱ አልጀመረም።\n"
            f"ቦቱን ከ <b>{DISPLAY_START}</b> ጀምሮ መጠቀም ይችላሉ።"
        )

    if reason == "sunday":
        return (
            "ዛሬ <b>እሁድ</b> ነው።\n"
            "የእረፍት ቀን ስለሆነ ቦቱ ዛሬ ለመደበኛ ተጠቃሚዎች አይገኝም።\n\n"
            f"እባክዎ በሚቀጥለው የሥራ ቀን <b>ሰኞ {DISPLAY_START}</b> ይሞክሩ።"
        )

    if reason == "friday_lunch":
        return (
            "ዛሬ <b>ዓርብ</b> የምሳ እረፍት ሰዓት ነው።\n"
            f"ቦቱ ከ <b>{DISPLAY_LUNCH}</b> ዝግ ይሆናል።\n\n"
            "እባክዎ ከ <b>1:30 PM</b> በኋላ እንደገና ይሞክሩ።"
        )

    if reason == "after":
        if weekday == 5:
            return (
                "የዛሬ የሥራ ሰዓት አልቋል።\n"
                "እሁድ የእረፍት ቀን ነው።\n"
                f"በሚቀጥለው የሥራ ቀን <b>ሰኞ {DISPLAY_START}</b> ይሞክሩ።"
            )

        next_day = NEXT_DAY_NAMES.get(weekday, "ሰኞ")

        return (
            "የዛሬ የሥራ ሰዓት አልቋል።\n"
            f"በሚቀጥለው የሥራ ቀን <b>{next_day} {DISPLAY_START}</b> ይሞክሩ።"
        )

    return ""


def get_closed_message():
    status = get_schedule_status()
    reason = status.get("reason", "")

    if reason == "sunday":
        message = (
            "🚫 <b>ቦቱ ዛሬ ለመደበኛ ተጠቃሚዎች ዝግ ነው።</b>\n\n"
            "ዛሬ <b>እሁድ</b> ነው። "
            "የእረፍት ቀን ስለሆነ ቦቱን መጠቀም አይችሉም።\n\n"
            f"እባክዎ በሚቀጥለው የሥራ ቀን <b>ሰኞ {DISPLAY_START}</b> ይሞክሩ።"
        )

    elif reason == "before":
        message = (
            "⏰ <b>የሥራ ሰዓቱ ገና አልጀመረም።</b>\n\n"
            f"እባክዎ ከ <b>{DISPLAY_START}</b> ጀምሮ ቦቱን ይጠቀሙ።"
        )

    elif reason == "friday_lunch":
        message = (
            "🍽️ <b>የዓርብ ምሳ እረፍት ሰዓት ነው።</b>\n\n"
            f"ቦቱ ከ <b>{DISPLAY_LUNCH}</b> ዝግ ነው።\n"
            "እባክዎ ከ <b>1:30 PM</b> በኋላ እንደገና ይሞክሩ።"
        )

    elif reason == "after":
        message = (
            "⏰ <b>የዛሬ የሥራ ሰዓት አልቋል።</b>\n\n"
            f"{get_next_working_period(status)}"
        )

    else:
        message = (
            "🚫 <b>ቦቱ በአሁኑ ሰዓት ለመደበኛ ተጠቃሚዎች አይገኝም።</b>"
        )

    return message + "\n\n" + get_working_hours_text()


async def check_user_working_hours(user_id):
    if is_admin_user(user_id):
        return True

    return bool(get_schedule_status()["working"])


async def deny_if_closed(update, context, end_conversation=False):
    user = update.effective_user

    if user is None or is_admin_user(user.id):
        return False

    if await check_user_working_hours(user.id):
        return False

    context.user_data.clear()

    await telegram_call(
        update.message.reply_text,
        get_closed_message(),
        parse_mode="HTML",
        reply_markup=start_keyboard(),
    )

    return True


# ============================================================
# ADMIN: DEBUG TIME / TEST SCHEDULE
# ============================================================

WEEKDAY_NAMES = {
    0: "Monday / ሰኞ",
    1: "Tuesday / ማክሰኞ",
    2: "Wednesday / ረቡዕ",
    3: "Thursday / ሐሙስ",
    4: "Friday / ዓርብ",
    5: "Saturday / ቅዳሜ",
    6: "Sunday / እሁድ",
}


async def debug_time_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    user = update.effective_user

    if user is None or user.id != ADMIN_ID:
        try:
            await telegram_call(update.message.reply_text, "❌ ፈቃድ የለዎትም።")
        except Exception:
            pass
        return

    try:
        now = get_utc_plus_3_now()
        status = get_real_schedule_status()
        schedule_result = "🟢 OPEN" if status["working"] else "🔴 CLOSED"
        weekday_name = WEEKDAY_NAMES.get(now.weekday(), "Unknown")

        await telegram_call(
            update.message.reply_text,
            "🕐 <b>BOT TIME DEBUG</b>\n\n"
            "🕒 <b>Current UTC+03:00 time:</b>\n"
            f"<code>{now.strftime('%Y-%m-%d %H:%M:%S %z')}</code>\n\n"
            "📅 <b>Day:</b>\n"
            f"<code>{weekday_name}</code>\n\n"
            "🌍 <b>Timezone:</b>\n"
            "<code>UTC+03:00</code>\n\n"
            "🟢 <b>Opening:</b> "
            f"<code>{SCHEDULE_START_HOUR:02d}:{SCHEDULE_START_MINUTE:02d}</code>\n"
            "🔴 <b>Closing:</b> "
            f"<code>{SCHEDULE_END_HOUR:02d}:{SCHEDULE_END_MINUTE:02d}</code>\n"
            "🍽️ <b>Friday lunch:</b> <code>11:30 - 13:30</code>\n\n"
            f"<b>Current schedule status:</b> {schedule_result}\n\n"
            f"<b>Reason:</b> <code>{status['reason']}</code>",
            parse_mode="HTML",
        )

    except Exception as error:
        logger.exception("Debug time command error: %s", error)


async def test_schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message is None:
        return

    user = update.effective_user

    if user is None or user.id != ADMIN_ID:
        try:
            await telegram_call(update.message.reply_text, "❌ ፈቃድ የለዎትም።")
        except Exception:
            pass
        return

    try:
        tests = [
            (0, 8, 29, "Monday 08:29", False),
            (0, 8, 30, "Monday 08:30", True),
            (0, 8, 45, "Monday 08:45", True),
            (0, 16, 59, "Monday 16:59", True),
            (0, 17, 0, "Monday 17:00", False),
            (4, 11, 29, "Friday 11:29", True),
            (4, 11, 30, "Friday 11:30", False),
            (4, 13, 29, "Friday 13:29", False),
            (4, 13, 30, "Friday 13:30", True),
            (5, 8, 29, "Saturday 08:29", False),
            (5, 8, 30, "Saturday 08:30", True),
            (5, 16, 59, "Saturday 16:59", True),
            (5, 17, 0, "Saturday 17:00", False),
            (6, 8, 45, "Sunday 08:45", False),
        ]

        lines = [
            "🧪 <b>SCHEDULE TEST</b>",
            "",
            "🌍 Timezone: <code>UTC+03:00</code>",
            "",
        ]

        all_ok = True

        for weekday, hour, minute, label, expected in tests:
            working = evaluate_schedule(weekday, hour, minute) == "working"
            ok = working == expected
            all_ok = all_ok and ok
            result = "✅ OPEN" if working else "❌ CLOSED"
            mark = "👍" if ok else "⚠️ UNEXPECTED"
            lines.append(f"<b>{label}</b> → {result} {mark}")

        lines.append("")
        lines.append(
            "All checks passed ✅" if all_ok else "Some checks failed ⚠️"
        )

        await telegram_call(
            update.message.reply_text,
            "\n".join(lines),
            parse_mode="HTML",
        )

    except Exception as error:
        logger.exception("Test schedule command error: %s", error)


# ============================================================
# RENDER HEALTH SERVER
# ============================================================

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

    def log_message(self, format, *args):
        return


def run_health_server():
    port = int(os.environ.get("PORT", "10000"))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    logger.info("Render health server running on port %s.", port)
    server.serve_forever()


# ============================================================
# CONVERSATION STATES / KEYS / LOCKS
# ============================================================

REGION, ZONE, DISTRICT, KEBELE, CONFIRM = range(5)

REGION_KEY = "region"
ZONE_KEY = "zone"
DISTRICT_KEY = "district"
KEBELE_KEY = "kebele"

MENU_BUTTONS_REGEX = r"^(▶️ ጀምር|▶️ START / ጀምር|START|❓ እገዛ)$"
START_BUTTON_REGEX = r"^(▶️ ጀምር|▶️ START / ጀምር|START)$"

USER_LOCKS = {}
USER_LOCKS_MASTER = asyncio.Lock()

GLOBAL_API_SEMAPHORE = asyncio.Semaphore(TELEGRAM_API_CONCURRENCY)

ADMIN_SEND_LOCKS = {}
ADMIN_SEND_LOCKS_MASTER = asyncio.Lock()


# ============================================================
# DATABASE
# ============================================================

def create_database_connection():
    connection = sqlite3.connect(DATABASE_FILE, timeout=30)
    cursor = connection.cursor()

    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()

    return connection


def database_operation(operation):
    delay = DATABASE_INITIAL_RETRY_DELAY

    for attempt in range(1, DATABASE_MAX_RETRIES + 1):
        try:
            return operation()

        except (sqlite3.OperationalError, sqlite3.DatabaseError) as error:
            error_text = str(error).lower()
            retryable = "locked" in error_text or "busy" in error_text

            if not retryable or attempt >= DATABASE_MAX_RETRIES:
                raise

            logger.warning(
                "SQLite temporary error (attempt %s/%s): %s",
                attempt,
                DATABASE_MAX_RETRIES,
                error,
            )

            wait = min(delay + random.uniform(0.05, 0.5), DATABASE_MAX_RETRY_DELAY)
            time.sleep(wait)
            delay = min(delay * 2, DATABASE_MAX_RETRY_DELAY)

    return None


async def db_async(func, *args, **kwargs):
    return await asyncio.to_thread(
        lambda: database_operation(lambda: func(*args, **kwargs))
    )


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def initialize_database_operation():
    with closing(create_database_connection()) as connection:
        cursor = connection.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                submission_hash TEXT NOT NULL,
                region TEXT NOT NULL,
                zone TEXT NOT NULL,
                district TEXT NOT NULL,
                kebele TEXT NOT NULL,
                telegram_message_id INTEGER,
                created_at TEXT NOT NULL,
                UNIQUE(user_id, submission_hash)
            )
            """
        )

        cursor.execute("PRAGMA table_info(submissions)")
        submission_columns = {row[1] for row in cursor.fetchall()}

        submission_migrations = {
            "telegram_message_id": (
                "ALTER TABLE submissions ADD COLUMN telegram_message_id INTEGER"
            ),
            "created_at": "ALTER TABLE submissions ADD COLUMN created_at TEXT",
        }

        for column, statement in submission_migrations.items():
            if column not in submission_columns:
                cursor.execute(statement)

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_seen TEXT NOT NULL,
                bot_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )

        cursor.execute("PRAGMA table_info(users)")
        user_columns = {row[1] for row in cursor.fetchall()}

        user_migrations = {
            "first_seen": "ALTER TABLE users ADD COLUMN first_seen TEXT",
            "bot_active": (
                "ALTER TABLE users ADD COLUMN bot_active INTEGER NOT NULL DEFAULT 1"
            ),
            "username": "ALTER TABLE users ADD COLUMN username TEXT",
            "first_name": "ALTER TABLE users ADD COLUMN first_name TEXT",
            "last_name": "ALTER TABLE users ADD COLUMN last_name TEXT",
            "created_at": "ALTER TABLE users ADD COLUMN created_at TEXT",
            "updated_at": "ALTER TABLE users ADD COLUMN updated_at TEXT",
        }

        for column, statement in user_migrations.items():
            if column not in user_columns:
                cursor.execute(statement)

        current_time = now_iso()

        cursor.execute("UPDATE users SET bot_active = 1 WHERE bot_active IS NULL")

        cursor.execute(
            "UPDATE users SET first_seen = ? WHERE first_seen IS NULL OR first_seen = ''",
            (current_time,),
        )

        cursor.execute(
            "UPDATE users SET created_at = first_seen "
            "WHERE created_at IS NULL OR created_at = ''"
        )

        cursor.execute(
            "UPDATE users SET updated_at = created_at "
            "WHERE updated_at IS NULL OR updated_at = ''"
        )

        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_submissions_user_hash
            ON submissions(user_id, submission_hash)
            """
        )

        connection.commit()


def initialize_database():
    database_operation(initialize_database_operation)
  # ------------------------------------------------------------
# User status / registration
# ------------------------------------------------------------

def get_user_status_operation(user_id):
    """Read-only: tells whether the user exists and is active."""
    with closing(create_database_connection()) as connection:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT user_id, bot_active FROM users WHERE user_id = ? LIMIT 1",
            (user_id,),
        )
        row = cursor.fetchone()

    if row is None:
        return {"exists": False, "active": True}
    return {"exists": True, "active": bool(row[1])}


def register_user_operation(user):
    if user is None:
        return

    now_text = now_iso()

    with closing(create_database_connection()) as connection:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT user_id FROM users WHERE user_id = ? LIMIT 1",
            (user.id,),
        )
        existing = cursor.fetchone()

        if existing is None:
            cursor.execute(
                """
                INSERT INTO users (
                    user_id, first_seen, bot_active,
                    username, first_name, last_name,
                    created_at, updated_at
                )
                VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (
                    user.id,
                    now_text,
                    user.username or None,
                    user.first_name or None,
                    user.last_name or None,
                    now_text,
                    now_text,
                ),
            )
        else:
            cursor.execute(
                """
                UPDATE users
                SET username = ?, first_name = ?, last_name = ?,
                    bot_active = 1, updated_at = ?
                WHERE user_id = ?
                """,
                (
                    user.username or None,
                    user.first_name or None,
                    user.last_name or None,
                    now_text,
                    user.id,
                ),
            )

        connection.commit()


def set_user_active_operation(user_id, active):
    with closing(create_database_connection()) as connection:
        cursor = connection.cursor()
        cursor.execute(
            "UPDATE users SET bot_active = ?, updated_at = ? WHERE user_id = ?",
            (1 if active else 0, now_iso(), user_id),
        )
        connection.commit()


async def get_user_status_async(user_id):
    return await db_async(get_user_status_operation, user_id)


async def register_user_async(user):
    await db_async(register_user_operation, user)


async def mark_user_active_async(user_id):
    await db_async(set_user_active_operation, user_id, True)


async def mark_user_inactive_async(user_id):
    await db_async(set_user_active_operation, user_id, False)


# ------------------------------------------------------------
# Locks
# ------------------------------------------------------------

async def get_user_lock(user_id):
    async with USER_LOCKS_MASTER:
        if user_id not in USER_LOCKS:
            USER_LOCKS[user_id] = asyncio.Lock()
        return USER_LOCKS[user_id]


async def get_admin_message_lock(chat_id, message_id):
    lock_key = (int(chat_id), int(message_id))
    async with ADMIN_SEND_LOCKS_MASTER:
        if lock_key not in ADMIN_SEND_LOCKS:
            ADMIN_SEND_LOCKS[lock_key] = asyncio.Lock()
        return ADMIN_SEND_LOCKS[lock_key]


# ------------------------------------------------------------
# Submissions
# ------------------------------------------------------------

def create_submission_hash(user_id, region, zone, district, kebele):
    normalized = "|".join(
        [
            str(user_id),
            region.strip().casefold(),
            zone.strip().casefold(),
            district.strip().casefold(),
            kebele.strip().casefold(),
        ]
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def submission_already_exists_operation(user_id, submission_hash):
    with closing(create_database_connection()) as connection:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT id FROM submissions
            WHERE user_id = ? AND submission_hash = ?
            LIMIT 1
            """,
            (user_id, submission_hash),
        )
        return cursor.fetchone() is not None


def save_submission_operation(
    user_id, submission_hash, region, zone, district, kebele, message_id
):
    with closing(create_database_connection()) as connection:
        cursor = connection.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO submissions (
                    user_id, submission_hash, region, zone, district,
                    kebele, telegram_message_id, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    submission_hash,
                    region,
                    zone,
                    district,
                    kebele,
                    message_id,
                    now_iso(),
                ),
            )
            connection.commit()
            return True
        except sqlite3.IntegrityError:
            connection.rollback()
            return False


# ============================================================
# SAFE TELEGRAM API CALL
# ============================================================

async def telegram_call(func, *args, **kwargs):
    """Call a Telegram API method with sensible retries.

    Retried : RetryAfter (flood control), TimedOut, real network errors.
    Raised immediately: BadRequest, Forbidden, any non-Telegram exception.

    NOTE: In python-telegram-bot, BadRequest is a subclass of NetworkError,
    so BadRequest/Forbidden MUST be handled before NetworkError.
    """
    delay = INITIAL_RETRY_DELAY
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            async with GLOBAL_API_SEMAPHORE:
                return await func(*args, **kwargs)

        except (BadRequest, Forbidden):
            raise

        except RetryAfter as error:
            last_error = error
            try:
                wait_time = max(float(error.retry_after), 1.0)
            except (TypeError, ValueError):
                wait_time = 1.0
            wait_time = min(wait_time + random.uniform(0.2, 1.0), MAX_RETRY_DELAY)

            logger.warning("Telegram rate limit. Waiting %.2f seconds.", wait_time)
            if attempt >= MAX_RETRIES:
                raise
            await asyncio.sleep(wait_time)

        except (TimedOut, NetworkError, TelegramError) as error:
            last_error = error
            logger.warning(
                "Telegram error (attempt %s/%s): %s", attempt, MAX_RETRIES, error
            )
            if attempt >= MAX_RETRIES:
                raise

            wait_time = min(delay + random.uniform(0.2, 1.5), MAX_RETRY_DELAY)
            await asyncio.sleep(wait_time)
            delay = min(delay * 2, MAX_RETRY_DELAY)

    if last_error is not None:
        raise last_error
    return None


# ============================================================
# KEYBOARDS
# ============================================================

def start_keyboard():
    return ReplyKeyboardMarkup(
        [["▶️ ጀምር", "❓ እገዛ"]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def confirmation_keyboard():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✈️ ላክ", callback_data="confirm_send"),
                InlineKeyboardButton("✏️ አርም", callback_data="edit_form"),
            ],
            [
                InlineKeyboardButton("❌ ሰርዝ", callback_data="cancel_form"),
            ],
        ]
    )


def group_admin_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("✈️ SEND", callback_data="admin_send")]]
    )


# ============================================================
# /get_group_id
# ============================================================

async def get_group_id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat is None:
        return

    chat = update.effective_chat
    user = update.effective_user

    if user is None or user.id != ADMIN_ID:
        if update.message is not None:
            try:
                await telegram_call(update.message.reply_text, "❌ ፈቃድ የለዎትም።")
            except Exception:
                pass
        return

    if chat.type == "private":
        if update.message is not None:
            try:
                await telegram_call(
                    update.message.reply_text,
                    "ℹ️ ይህን ትዕዛዝ የቡድኑን ID ለማግኘት በDARS ቡድን ውስጥ ይጠቀሙ።\n\n"
                    "ቦቱን ወደ ቡድኑ ያክሉ እና <code>/get_group_id</code> ይላኩ።\n\n"
                    f"Configured group ID: <code>{GROUP_CHAT_ID}</code>",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return

    if chat.type not in {"group", "supergroup"}:
        return

    group_title = chat.title or "Unnamed Group"
    logger.info(
        "GROUP ID REQUESTED: %s | Group: %s | Type: %s",
        chat.id,
        group_title,
        chat.type,
    )

    if update.message is None:
        return

    try:
        await telegram_call(
            update.message.reply_text,
            "🆔 <b>DARS Group Information</b>\n\n"
            f"👥 <b>Group:</b> {html.escape(group_title)}\n"
            f"🆔 <b>Chat ID:</b> <code>{chat.id}</code>\n"
            f"📌 <b>Type:</b> {chat.type}\n\n"
            "Use this Chat ID in your code as <code>GROUP_CHAT_ID</code>.",
            parse_mode="HTML",
        )
    except Exception as error:
        logger.exception("Could not send group ID: %s", error)


# ============================================================
# /start
# ============================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat is None or update.effective_chat.type != "private":
        return
    if update.message is None:
        return

    try:
        user = update.effective_user
        if user is None:
            return
        user_id = user.id

        user_status = await get_user_status_async(user_id)
        await register_user_async(user)

        if await deny_if_closed(update, context):
            return

        context.user_data.clear()

        should_show_welcome = (
            not user_status["exists"] or not user_status["active"]
        )

        if should_show_welcome:
            await telegram_call(
                update.message.reply_text,
                "👋 <b>እንኳን ወደ DARS አድራሻ መላኪያ ቦት በደህና መጡ!</b>\n\n"
                "ይህ ቦት የአድራሻ መረጃዎን በቀላሉ ለDARS ለመላክ ያግዝዎታል።\n\n"
                "አድራሻዎን ለመላክ «▶️ ጀምር» የሚለውን ይጫኑ።\n\n"
                "እንዴት እንደሚጠቀሙ ለማወቅ «❓ እገዛ» የሚለውን ይጫኑ።",
                parse_mode="HTML",
                reply_markup=start_keyboard(),
            )
            return

        await telegram_call(
            update.message.reply_text,
            "📍 <b>የDARS አድራሻ መላኪያ</b>\n\n"
            "መረጃዎን ለማስገባት «▶️ ጀምር» የሚለውን ይጫኑ።",
            parse_mode="HTML",
            reply_markup=start_keyboard(),
        )

    except Exception as error:
        logger.exception("Start command error: %s", error)


# ============================================================
# BOT MEMBERSHIP STATUS
# ============================================================

async def bot_membership_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        change = update.my_chat_member
        if change is None or change.chat.type != "private":
            return

        user_id = change.chat.id
        new_status = change.new_chat_member.status
        old_status = change.old_chat_member.status

        logger.info(
            "Bot membership changed for user %s: %s -> %s",
            user_id,
            old_status,
            new_status,
        )

        if new_status == "kicked":
            await mark_user_inactive_async(user_id)
            logger.info("User %s marked inactive.", user_id)

        elif new_status == "member":
            logger.info("User %s is active again.", user_id)

    except Exception as error:
        logger.exception("Bot membership update error: %s", error)


# ============================================================
# HELP
# ============================================================

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat is None or update.effective_chat.type != "private":
        return
    if update.message is None:
        return

    try:
        await telegram_call(
            update.message.reply_text,
            "❓ <b>የDARS አድራሻ መላኪያ ቦት — አጠቃቀም</b>\n\n"
            "1️⃣ «▶️ ጀምር» የሚለውን ይጫኑ።\n\n"
            "2️⃣ የሚጠየቁትን የአድራሻ መረጃዎች ያስገቡ፦\n"
            "• ክልል\n"
            "• ዞን / ከተማ\n"
            "• ወረዳ / ክፍለ ከተማ\n"
            "• ቀበሌ / ወረዳ\n\n"
            "3️⃣ ያስገቡትን መረጃ ይመልከቱ።\n\n"
            "4️⃣ መረጃው ትክክል ከሆነ «✈️ ላክ» ይጫኑ።\n\n"
            "5️⃣ ለማስተካከል «✏️ አርም» ይጫኑ።\n\n"
            "6️⃣ ለመሰረዝ «❌ ሰርዝ» ይጫኑ።\n\n"
            "⚠️ አድራሻው ከተላከ በኋላ ወደ DARS ቡድን ይላካል።\n\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            + get_working_hours_text()
            + "\n\n━━━━━━━━━━━━━━━━━━\n\n"
            "ሌላ አድራሻ ለመላክ «▶️ ጀምር» ይጫኑ።",
            parse_mode="HTML",
            reply_markup=start_keyboard(),
        )

    except Exception as error:
        logger.exception("Help command error: %s", error)


# ============================================================
# FORM
# ============================================================

async def start_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat is None or update.effective_chat.type != "private":
        return ConversationHandler.END
    if update.message is None:
        return ConversationHandler.END

    try:
        user = update.effective_user
        if user is None:
            return ConversationHandler.END

        await register_user_async(user)

        if await deny_if_closed(update, context):
            return ConversationHandler.END

        context.user_data.clear()

        await telegram_call(
            update.message.reply_text,
            "1️⃣ 🗺️ <b>ክልል</b>\n\nእባክዎ ክልል ያስገቡ።",
            parse_mode="HTML",
            reply_markup=ReplyKeyboardRemove(),
        )

        return REGION

    except Exception as error:
        logger.exception("Start form error: %s", error)
        return ConversationHandler.END


def make_form_step(state, key, error_text, next_state, next_prompt):
    """Builds the handler for one text step of the form."""

    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_chat is None or update.effective_chat.type != "private":
            return state
        if update.message is None or update.message.text is None:
            return state
        if update.effective_user is None:
            return state

        try:
            if await deny_if_closed(update, context):
                return ConversationHandler.END

            text = update.message.text.strip()

            if not text:
                await telegram_call(update.message.reply_text, error_text)
                return state

            context.user_data[key] = text

            await telegram_call(
                update.message.reply_text,
                next_prompt,
                parse_mode="HTML",
            )

            return next_state

        except Exception as error:
            logger.exception("Form step error (%s): %s", key, error)
            return state

    return handler


receive_region = make_form_step(
    REGION,
    REGION_KEY,
    "❌ እባክዎ ትክክለኛ ክልል ያስገቡ።",
    ZONE,
    "2️⃣ 📍 <b>ዞን / ከተማ</b>\n\nእባክዎ ዞን ወይም ከተማ ያስገቡ።",
)

receive_zone = make_form_step(
    ZONE,
    ZONE_KEY,
    "❌ እባክዎ ትክክለኛ ዞን ወይም ከተማ ያስገቡ።",
    DISTRICT,
    "3️⃣ 🏙️ <b>ወረዳ / ክፍለ ከተማ</b>\n\nእባክዎ ወረዳ ወይም ክፍለ ከተማ ያስገቡ።",
)

receive_district = make_form_step(
    DISTRICT,
    DISTRICT_KEY,
    "❌ እባክዎ ትክክለኛ ወረዳ ወይም ክፍለ ከተማ ያስገቡ።",
    KEBELE,
    "4️⃣ 🏠 <b>ቀበሌ / ወረዳ</b>\n\nእባክዎ ቀበሌ ወይም ወረዳ ያስገቡ።",
)


async def receive_kebele(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat is None or update.effective_chat.type != "private":
        return KEBELE
    if update.message is None or update.message.text is None:
        return KEBELE
    if update.effective_user is None:
        return KEBELE

    try:
        if await deny_if_closed(update, context):
            return ConversationHandler.END

        text = update.message.text.strip()

        if not text:
            await telegram_call(
                update.message.reply_text,
                "❌ እባክዎ ትክክለኛ ቀበሌ ወይም ወረዳ ያስገቡ።",
            )
            return KEBELE

        context.user_data[KEBELE_KEY] = text

        def field(key):
            return html.escape(context.user_data.get(key, "አልተገኘም"))

        review_text = (
            "📋 <b>መረጃዎን ያረጋግጡ</b>\n\n"
            f"🗺️ <b>ክልል:</b> {field(REGION_KEY)}\n"
            f"📍 <b>ዞን / ከተማ:</b> {field(ZONE_KEY)}\n"
            f"🏙️ <b>ወረዳ / ክፍለ ከተማ:</b> {field(DISTRICT_KEY)}\n"
            f"🏠 <b>ቀበሌ / ወረዳ:</b> {field(KEBELE_KEY)}\n\n"
            "መረጃው ትክክል ከሆነ «✈️ ላክ» የሚለውን ይጫኑ።\n"
            "ለማስተካከል «✏️ አርም» ይጫኑ።"
        )

        await telegram_call(
            update.message.reply_text,
            review_text,
            parse_mode="HTML",
            reply_markup=confirmation_keyboard(),
        )

        return CONFIRM

    except Exception as error:
        logger.exception("Receive kebele error: %s", error)
        return KEBELE
    # ============================================================
# CONFIRM SEND
# ============================================================

async def confirm_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query is None:
        return ConversationHandler.END

    if query.message is None or query.message.chat.type != "private":
        try:
            await query.answer()
        except Exception:
            pass
        return ConversationHandler.END

    user = query.from_user
    user_id = user.id

    lock = await get_user_lock(user_id)

    async with lock:
        try:
            await query.answer()
        except Exception:
            pass

        try:
            if not is_admin_user(user_id):
                if not await check_user_working_hours(user_id):
                    context.user_data.clear()

                    try:
                        await telegram_call(
                            query.message.edit_text,
                            get_closed_message(),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass

                    await telegram_call(
                        query.message.reply_text,
                        "እንደገና ለመጀመር «▶️ ጀምር» የሚለውን ይጫኑ።",
                        reply_markup=start_keyboard(),
                    )

                    return ConversationHandler.END

            region = context.user_data.get(REGION_KEY, "").strip()
            zone = context.user_data.get(ZONE_KEY, "").strip()
            district = context.user_data.get(DISTRICT_KEY, "").strip()
            kebele = context.user_data.get(KEBELE_KEY, "").strip()

            if not all([region, zone, district, kebele]):
                await telegram_call(
                    query.message.reply_text,
                    "❌ መረጃው ሙሉ አይደለም። እባክዎ እንደገና ይጀምሩ።",
                    reply_markup=start_keyboard(),
                )

                context.user_data.clear()
                return ConversationHandler.END

            submission_hash = create_submission_hash(
                user_id, region, zone, district, kebele
            )

            if await db_async(
                submission_already_exists_operation, user_id, submission_hash
            ):
                try:
                    await telegram_call(
                        query.message.edit_text,
                        "⚠️ <b>ይህ አድራሻ ቀደም ሲል ተልኳል።</b>\n\n"
                        "ተመሳሳይ አድራሻን እንደገና መላክ አያስፈልግም።",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

                await telegram_call(
                    query.message.reply_text,
                    "ሌላ አድራሻ ለመላክ «▶️ ጀምር» የሚለውን ይጫኑ።",
                    reply_markup=start_keyboard(),
                )

                context.user_data.clear()
                return ConversationHandler.END

            try:
                await telegram_call(
                    query.message.edit_reply_markup,
                    reply_markup=None,
                )
            except Exception:
                pass

            full_name = user.full_name or "አልተገኘም"
            username = f"@{user.username}" if user.username else "አልተገኘም"

            group_text = (
                "📍 <b>የDARS አድራሻ ማስገቢያ</b>\n\n"
                f"👤 <b>ስም:</b> {html.escape(full_name)}\n"
                f"🔹 <b>የተጠቃሚ ስም:</b> {html.escape(username)}\n\n"
                f"🗺️ <b>ክልል:</b> {html.escape(region)}\n"
                f"📍 <b>ዞን / ከተማ:</b> {html.escape(zone)}\n"
                f"🏙️ <b>ወረዳ / ክፍለ ከተማ:</b> {html.escape(district)}\n"
                f"🏠 <b>ቀበሌ / ወረዳ:</b> {html.escape(kebele)}"
            )

            try:
                sent_message = await telegram_call(
                    context.bot.send_message,
                    chat_id=GROUP_CHAT_ID,
                    text=group_text,
                    parse_mode="HTML",
                    reply_markup=group_admin_keyboard(),
                )

            except Forbidden:
                logger.exception(
                    "Bot is not allowed to send messages to the DARS group."
                )

                await telegram_call(
                    query.message.reply_text,
                    "❌ መረጃውን ወደ DARS ቡድን መላክ አልተቻለም።",
                    reply_markup=start_keyboard(),
                )

                context.user_data.clear()
                return ConversationHandler.END

            except Exception as error:
                logger.exception("Could not send submission: %s", error)

                await telegram_call(
                    query.message.reply_text,
                    "❌ መረጃውን መላክ አልተቻለም።\n\n"
                    "እባክዎ ከጥቂት ጊዜ በኋላ እንደገና ይሞክሩ።",
                    reply_markup=start_keyboard(),
                )

                context.user_data.clear()
                return ConversationHandler.END

            try:
                saved = await db_async(
                    save_submission_operation,
                    user_id,
                    submission_hash,
                    region,
                    zone,
                    district,
                    kebele,
                    sent_message.message_id,
                )

                if not saved:
                    logger.warning(
                        "Duplicate submission detected while saving user %s.",
                        user_id,
                    )

            except Exception as error:
                logger.exception("Could not save submission record: %s", error)

            try:
                await telegram_call(
                    query.message.edit_text,
                    "✅ <b>አድራሻው በተሳካ ሁኔታ ተልኳል።</b>\n\n"
                    "መረጃዎ ወደ DARS ቡድን ተልኳል።",
                    parse_mode="HTML",
                )
            except Exception:
                pass

            await telegram_call(
                query.message.reply_text,
                "ሌላ አድራሻ ለመላክ «▶️ ጀምር» የሚለውን ይጫኑ።",
                reply_markup=start_keyboard(),
            )

            context.user_data.clear()
            return ConversationHandler.END

        except Exception as error:
            logger.exception(
                "Unexpected confirm_send error for user %s: %s",
                user_id,
                error,
            )

            try:
                context.user_data.clear()

                await telegram_call(
                    query.message.reply_text,
                    "❌ ሂደቱ ላይ ጊዜያዊ ችግር ተፈጥሯል።\n\n"
                    "እባክዎ «▶️ ጀምር» በመጫን እንደገና ይሞክሩ።",
                    reply_markup=start_keyboard(),
                )

            except Exception:
                pass

            return ConversationHandler.END


# ============================================================
# EDIT / CANCEL FORM
# ============================================================

async def edit_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query is None:
        return ConversationHandler.END

    if query.message is None or query.message.chat.type != "private":
        try:
            await query.answer()
        except Exception:
            pass
        return ConversationHandler.END

    user_id = query.from_user.id

    try:
        await query.answer()
    except Exception:
        pass

    if not is_admin_user(user_id):
        if not await check_user_working_hours(user_id):
            context.user_data.clear()

            try:
                await telegram_call(
                    query.message.edit_text,
                    get_closed_message(),
                    parse_mode="HTML",
                )
            except Exception:
                pass

            await telegram_call(
                query.message.reply_text,
                "እንደገና ለመጀመር «▶️ ጀምር» የሚለውን ይጫኑ።",
                reply_markup=start_keyboard(),
            )

            return ConversationHandler.END

    try:
        context.user_data.clear()

        await telegram_call(
            query.message.edit_text,
            "✏️ <b>ማስተካከያ</b>\n\nእባክዎ ክልል እንደገና ያስገቡ።",
            parse_mode="HTML",
        )

        return REGION

    except Exception as error:
        logger.exception("Edit form error: %s", error)
        return ConversationHandler.END


async def cancel_form(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query is None:
        return ConversationHandler.END

    if query.message is None or query.message.chat.type != "private":
        try:
            await query.answer()
        except Exception:
            pass
        return ConversationHandler.END

    try:
        await query.answer()
    except Exception:
        pass

    try:
        context.user_data.clear()

        await telegram_call(
            query.message.edit_text,
            "❌ <b>ማስገባቱ ተሰርዟል።</b>",
            parse_mode="HTML",
        )

        await telegram_call(
            query.message.reply_text,
            "እንደገና ለመጀመር «▶️ ጀምር» የሚለውን ይጫኑ።",
            reply_markup=start_keyboard(),
        )

        return ConversationHandler.END

    except Exception as error:
        logger.exception("Cancel form error: %s", error)
        return ConversationHandler.END


# ============================================================
# GROUP ADMIN "SEND" BUTTON
# ============================================================

async def admin_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query is None:
        return

    if query.from_user.id != ADMIN_ID:
        try:
            await telegram_call(
                query.answer,
                "❌ Unauthorized",
                show_alert=True,
            )
        except Exception:
            pass
        return

    if query.message is None or query.message.chat.id != GROUP_CHAT_ID:
        try:
            await telegram_call(
                query.answer,
                "❌ Unauthorized",
                show_alert=True,
            )
        except Exception:
            pass
        return

    message_lock = await get_admin_message_lock(
        query.message.chat.id,
        query.message.message_id,
    )

    async with message_lock:
        try:
            await telegram_call(query.answer, "Sent")
        except Exception:
            pass

        original_text = query.message.text_html or ""

        if "✅ Sent" in original_text:
            try:
                if query.message.reply_markup:
                    await telegram_call(
                        query.edit_message_reply_markup,
                        reply_markup=None,
                    )
            except Exception:
                pass
            return

        new_text = f"{original_text}\n\n✅ Sent"

        try:
            await telegram_call(
                query.edit_message_text,
                text=new_text,
                parse_mode="HTML",
                reply_markup=None,
            )

            logger.info(
                "Message %s marked as Sent.",
                query.message.message_id,
            )

        except BadRequest as error:
            error_text = str(error).lower()

            if "message is not modified" in error_text:
                logger.info(
                    "Message %s was already processed.",
                    query.message.message_id,
                )
                return

            if "message to edit not found" in error_text:
                logger.warning(
                    "Message %s no longer exists.",
                    query.message.message_id,
                )
                return

            logger.warning("Admin SEND BadRequest: %s", error)

        except Forbidden:
            logger.exception(
                "Bot does not have permission to edit messages in the DARS group."
            )

        except Exception as error:
            logger.exception("Admin SEND error: %s", error)


# ============================================================
# /cancel, /admin, unknown command
# ============================================================

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat is None or update.effective_chat.type != "private":
        return ConversationHandler.END

    if update.message is None:
        return ConversationHandler.END

    try:
        context.user_data.clear()

        await telegram_call(
            update.message.reply_text,
            "❌ ተሰርዟል።\n\nእንደገና ለመጀመር «▶️ ጀምር» የሚለውን ይጫኑ።",
            reply_markup=start_keyboard(),
        )

    except Exception as error:
        logger.exception("Cancel command error: %s", error)

    return ConversationHandler.END


async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat is None or update.effective_chat.type != "private":
        return

    if update.message is None:
        return

    if update.effective_user is None or update.effective_user.id != ADMIN_ID:
        try:
            await telegram_call(
                update.message.reply_text,
                "❌ ፈቃድ የለዎትም።",
            )
        except Exception:
            pass
        return

    try:
        status = get_real_schedule_status()

        schedule_status = (
            "🟢 አሁን የሥራ ሰዓት ነው።"
            if status["working"]
            else "🔴 አሁን ከመደበኛ የሥራ ሰዓት ውጭ ነው።"
        )

        await telegram_call(
            update.message.reply_text,
            "✅ <b>እርስዎ የDARS ቦት አስተዳዳሪ ነዎት።</b>\n\n"
            "👑 አስተዳዳሪ ስለሆኑ ቦቱን በማንኛውም ሰዓት መጠቀም ይችላሉ።\n\n"
            f"{schedule_status}\n\n" + get_working_hours_text(),
            parse_mode="HTML",
        )

    except Exception as error:
        logger.exception("Admin command error: %s", error)


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat is None or update.effective_chat.type != "private":
        return

    if update.message is None or update.effective_user is None:
        return

    try:
        if await deny_if_closed(update, context):
            return

        await telegram_call(
            update.message.reply_text,
            "እባክዎ «▶️ ጀምር» የሚለውን ይጫኑ።",
            reply_markup=start_keyboard(),
        )

    except Exception as error:
        logger.exception("Unknown command error: %s", error)


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(
        "Unhandled bot error: %s",
        context.error,
        exc_info=context.error,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    health_thread = threading.Thread(
        target=run_health_server,
        daemon=True,
    )
    health_thread.start()

    initialize_database()

    logger.info("Database initialized successfully.")

    logger.info(
        "Schedule (UTC+03:00): %02d:%02d-%02d:%02d Mon-Sat, "
        "Friday lunch 11:30-13:30, Sunday closed.",
        SCHEDULE_START_HOUR,
        SCHEDULE_START_MINUTE,
        SCHEDULE_END_HOUR,
        SCHEDULE_END_MINUTE,
    )

    logger.info(
        "Current UTC+03:00 time: %s",
        get_utc_plus_3_now().strftime("%Y-%m-%d %H:%M:%S %z"),
    )

    logger.info("Admin ID %s has unrestricted access.", ADMIN_ID)
    logger.info("Configured GROUP_CHAT_ID: %s", GROUP_CHAT_ID)

    request = HTTPXRequest(
        connection_pool_size=CONNECTION_POOL_SIZE,
        connect_timeout=CONNECT_TIMEOUT,
        read_timeout=READ_TIMEOUT,
        write_timeout=WRITE_TIMEOUT,
        pool_timeout=POOL_TIMEOUT,
        media_write_timeout=MEDIA_WRITE_TIMEOUT,
        http_version="1.1",
    )

    get_updates_request = HTTPXRequest(
        connection_pool_size=GET_UPDATES_POOL_SIZE,
        connect_timeout=CONNECT_TIMEOUT,
        read_timeout=READ_TIMEOUT,
        write_timeout=WRITE_TIMEOUT,
        pool_timeout=POOL_TIMEOUT,
        media_write_timeout=MEDIA_WRITE_TIMEOUT,
        http_version="1.1",
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .get_updates_request(get_updates_request)
        .concurrent_updates(MAX_CONCURRENT_USERS)
        .build()
    )

    private = filters.ChatType.PRIVATE

    form_text = (
        private
        & filters.TEXT
        & ~filters.COMMAND
        & ~filters.Regex(MENU_BUTTONS_REGEX)
    )

    application.add_handler(
        ChatMemberHandler(
            bot_membership_update,
            ChatMemberHandler.MY_CHAT_MEMBER,
        )
    )

    application.add_handler(
        CommandHandler("get_group_id", get_group_id_command)
    )

    application.add_handler(
        CommandHandler("groupid", get_group_id_command)
    )

    application.add_handler(
        CommandHandler(
            "debug_time",
            debug_time_command,
            filters=private,
        )
    )

    application.add_handler(
        CommandHandler(
            "test_schedule",
            test_schedule_command,
            filters=private,
        )
    )

    conversation_handler = ConversationHandler(
        entry_points=[
            MessageHandler(
                private & filters.Regex(START_BUTTON_REGEX),
                start_form,
            )
        ],
        states={
            REGION: [
                MessageHandler(form_text, receive_region)
            ],
            ZONE: [
                MessageHandler(form_text, receive_zone)
            ],
            DISTRICT: [
                MessageHandler(form_text, receive_district)
            ],
            KEBELE: [
                MessageHandler(form_text, receive_kebele)
            ],
            CONFIRM: [
                CallbackQueryHandler(
                    confirm_send,
                    pattern=r"^confirm_send$",
                ),
                CallbackQueryHandler(
                    edit_form,
                    pattern=r"^edit_form$",
                ),
                CallbackQueryHandler(
                    cancel_form,
                    pattern=r"^cancel_form$",
                ),
            ],
        },
        fallbacks=[
            CommandHandler(
                "cancel",
                cancel_command,
                filters=private,
            ),
            MessageHandler(
                private & filters.Regex(START_BUTTON_REGEX),
                start_form,
            ),
        ],
        allow_reentry=True,
        per_chat=True,
        per_user=True,
        per_message=False,
    )

    application.add_handler(
        CommandHandler("start", start_command, filters=private)
    )

    application.add_handler(
        MessageHandler(
            private & filters.Regex(r"^❓ እገዛ$"),
            help_command,
        )
    )

    application.add_handler(
        CommandHandler("help", help_command, filters=private)
    )

    application.add_handler(conversation_handler)

    application.add_handler(
        CallbackQueryHandler(
            admin_send,
            pattern=r"^admin_send$",
        )
    )

    application.add_handler(
        CommandHandler("admin", admin_command, filters=private)
    )

    application.add_handler(
        CommandHandler("cancel", cancel_command, filters=private)
    )

    application.add_handler(
        MessageHandler(
            private & filters.COMMAND,
            unknown_command,
        )
    )

    application.add_error_handler(error_handler)

    print("DARS Location Bot is running...")
    logger.info("DARS Location Bot starting.")

    application.run_polling(
        drop_pending_updates=False,
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=-1,
    )


if __name__ == "__main__":
    main()  