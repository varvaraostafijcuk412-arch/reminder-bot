"""
Telegram Reminder Bot
Требования: python-telegram-bot>=20.0, apscheduler, python-dotenv
"""

import asyncio
import logging
import os
import re
import sqlite3
from datetime import datetime
from logging.handlers import RotatingFileHandler

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

# ─────────────────────────── Конфигурация ───────────────────────────

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_FILE = "reminders.db"

# ─────────────────────────── Логирование ────────────────────────────

def setup_logging() -> logging.Logger:
    logger = logging.getLogger("reminder_bot")
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Файл (ротация 5 МБ, 3 копии)
    fh = RotatingFileHandler("bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)

    # Консоль
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger


log = setup_logging()

# ─────────────────────────── База данных ────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def db_init() -> None:
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id   INTEGER NOT NULL,
                time      TEXT    NOT NULL,
                text      TEXT    NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
    log.info("База данных инициализирована: %s", DB_FILE)


def db_add_reminder(user_id: int, time: str, text: str) -> int:
    with db_connect() as conn:
        cur = conn.execute(
            "INSERT INTO reminders (user_id, time, text) VALUES (?, ?, ?)",
            (user_id, time, text),
        )
        return cur.lastrowid


def db_get_reminders(user_id: int) -> list[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM reminders WHERE user_id = ? AND is_active = 1 ORDER BY time",
            (user_id,),
        ).fetchall()


def db_get_all_active() -> list[sqlite3.Row]:
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM reminders WHERE is_active = 1"
        ).fetchall()


def db_delete_reminder(reminder_id: int, user_id: int) -> bool:
    with db_connect() as conn:
        cur = conn.execute(
            "DELETE FROM reminders WHERE id = ? AND user_id = ?",
            (reminder_id, user_id),
        )
        return cur.rowcount > 0


def db_clear_reminders(user_id: int) -> int:
    with db_connect() as conn:
        cur = conn.execute(
            "DELETE FROM reminders WHERE user_id = ?",
            (user_id,),
        )
        return cur.rowcount


def db_get_reminder_by_id(reminder_id: int) -> sqlite3.Row | None:
    with db_connect() as conn:
        return conn.execute(
            "SELECT * FROM reminders WHERE id = ? AND is_active = 1",
            (reminder_id,),
        ).fetchone()


# ─────────────────────────── Планировщик ────────────────────────────

scheduler = AsyncIOScheduler(timezone="Asia/Yakutsk")


def make_job_id(user_id: int, reminder_id: int) -> str:
    return f"reminder_{user_id}_{reminder_id}"


async def send_reminder(app: Application, user_id: int, text: str, reminder_id: int) -> None:
    """Коллбэк планировщика — отправляет напоминание пользователю."""
    try:
        await app.bot.send_message(
            chat_id=user_id,
            text=f"🔔 *Напоминание*\n\n{text}",
            parse_mode="Markdown",
        )
        log.info("Напоминание отправлено: user=%s, reminder_id=%s", user_id, reminder_id)
    except Exception as exc:
        log.error("Ошибка отправки напоминания user=%s id=%s: %s", user_id, reminder_id, exc)


def schedule_reminder(app: Application, user_id: int, reminder_id: int, time_str: str, text: str) -> None:
    """Добавляет задачу в планировщик."""
    hour, minute = map(int, time_str.split(":"))
    job_id = make_job_id(user_id, reminder_id)

    # Убираем старую задачу с тем же ID (на случай перезагрузки)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)

    scheduler.add_job(
        send_reminder,
        trigger=CronTrigger(hour=hour, minute=minute, timezone="Asia/Yakutsk"),
        args=[app, user_id, text, reminder_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=60,
    )
    log.info("Запланировано: job_id=%s, time=%s", job_id, time_str)


def unschedule_reminder(user_id: int, reminder_id: int) -> None:
    job_id = make_job_id(user_id, reminder_id)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        log.info("Удалено из планировщика: job_id=%s", job_id)


def load_all_reminders(app: Application) -> None:
    """Загружает все активные напоминания из БД в планировщик при старте."""
    rows = db_get_all_active()
    for row in rows:
        schedule_reminder(app, row["user_id"], row["id"], row["time"], row["text"])
    log.info("Загружено напоминаний из БД: %d", len(rows))


# ─────────────────────────── Утилиты ────────────────────────────────

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def parse_time(raw: str) -> str | None:
    """Возвращает нормализованное 'HH:MM' или None при ошибке."""
    raw = raw.strip()
    # Попробуем дополнить до HH:MM
    if re.match(r"^\d:\d{2}$", raw):
        raw = "0" + raw
    if TIME_RE.match(raw):
        return raw
    return None


def format_reminder_list(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "У вас нет активных напоминаний."
    lines = ["📋 *Ваши напоминания:*\n"]
    for i, row in enumerate(rows, 1):
        lines.append(f"{i}. 🕐 `{row['time']}` — {row['text']}  (ID: {row['id']})")
    return "\n".join(lines)


def build_list_keyboard(rows: list[sqlite3.Row]) -> InlineKeyboardMarkup:
    buttons = []
    for row in rows:
        buttons.append(
            [InlineKeyboardButton(
                text=f"🗑 Удалить: {row['time']} — {row['text'][:30]}",
                callback_data=f"del_{row['id']}",
            )]
        )
    return InlineKeyboardMarkup(buttons)


HELP_TEXT = (
    "👋 *Бот-напоминалка*\n\n"
    "Доступные команды:\n"
    "• /add `[HH:MM]` `[текст]` — добавить ежедневное напоминание\n"
    "  _Пример:_ `/add 21:00 Принять лекарства`\n"
    "• /list — показать все активные напоминания\n"
    "• /del `[номер или ID]` — удалить напоминание\n"
    "  _Пример:_ `/del 1`\n"
    "• /clear — удалить ВСЕ напоминания\n"
    "• /test `[HH:MM]` — тестовое напоминание (пришлёт через 5 сек)\n"
    "• /help — эта справка\n\n"
    "⏰ Все напоминания повторяются каждый день."
)

# ────────────────────── Обработчики команд ──────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode="Markdown")


async def cmd_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    args = ctx.args  # список слов после команды

    if not args or len(args) < 2:
        await update.message.reply_text(
            "⚠️ Использование: `/add HH:MM текст напоминания`\n"
            "_Пример:_ `/add 21:00 Принять лекарства`",
            parse_mode="Markdown",
        )
        return

    time_str = parse_time(args[0])
    if not time_str:
        await update.message.reply_text(
            f"❌ Некорректное время: `{args[0]}`\n"
            "Используйте 24-часовой формат: `HH:MM` (например, `09:30` или `21:00`).",
            parse_mode="Markdown",
        )
        return

    text = " ".join(args[1:]).strip()
    if not text:
        await update.message.reply_text("⚠️ Укажите текст напоминания.")
        return

    user_id = update.effective_user.id
    reminder_id = db_add_reminder(user_id, time_str, text)
    schedule_reminder(ctx.application, user_id, reminder_id, time_str, text)

    await update.message.reply_text(
        f"✅ Напоминание добавлено!\n"
        f"🕐 Время: *{time_str}* ежедневно\n"
        f"📝 Текст: {text}",
        parse_mode="Markdown",
    )
    log.info("Добавлено напоминание: user=%s id=%s time=%s", user_id, reminder_id, time_str)


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = db_get_reminders(user_id)

    text = format_reminder_list(rows)
    if rows:
        keyboard = build_list_keyboard(rows)
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await update.message.reply_text(text)


async def cmd_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if not ctx.args:
        await update.message.reply_text(
            "⚠️ Использование: `/del [номер]`\n"
            "Номер можно узнать через /list.",
            parse_mode="Markdown",
        )
        return

    raw = ctx.args[0]
    if not raw.isdigit():
        await update.message.reply_text("❌ Укажите числовой ID напоминания.")
        return

    # Сначала пробуем удалить по позиции из списка пользователя
    rows = db_get_reminders(user_id)
    num = int(raw)

    reminder_id: int | None = None

    if 1 <= num <= len(rows):
        # Это порядковый номер из /list
        reminder_id = rows[num - 1]["id"]
    else:
        # Может быть, это прямой ID из БД
        row = db_get_reminder_by_id(num)
        if row and row["user_id"] == user_id:
            reminder_id = num

    if reminder_id is None:
        await update.message.reply_text(
            f"❌ Напоминание #{raw} не найдено. Проверьте список через /list."
        )
        return

    deleted = db_delete_reminder(reminder_id, user_id)
    if deleted:
        unschedule_reminder(user_id, reminder_id)
        await update.message.reply_text(f"🗑 Напоминание #{raw} удалено.")
        log.info("Удалено напоминание: user=%s id=%s", user_id, reminder_id)
    else:
        await update.message.reply_text("❌ Не удалось удалить напоминание.")


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    rows = db_get_reminders(user_id)

    # Снимаем с планировщика до удаления из БД
    for row in rows:
        unschedule_reminder(user_id, row["id"])

    count = db_clear_reminders(user_id)
    await update.message.reply_text(
        f"🗑 Удалено {count} напоминаний." if count else "У вас не было активных напоминаний."
    )
    log.info("Очищены напоминания: user=%s, count=%s", user_id, count)


async def cmd_test(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Отправляет тестовое напоминание через ~5 секунд."""
    user_id = update.effective_user.id

    test_text = "🧪 Тестовое напоминание — всё работает!"
    if ctx.args:
        time_str = parse_time(ctx.args[0])
        if not time_str:
            await update.message.reply_text(
                f"❌ Некорректное время: `{ctx.args[0]}`",
                parse_mode="Markdown",
            )
            return
        test_text += f"\n_Было бы отправлено в {time_str}_"

    await update.message.reply_text(
        "⏳ Тестовое напоминание придёт через 5 секунд..."
    )

    # Разовая задача через 5 секунд
    from apscheduler.triggers.date import DateTrigger
    from datetime import timedelta

    fire_at = datetime.now(scheduler.timezone) + timedelta(seconds=5)
    scheduler.add_job(
        send_reminder,
        trigger=DateTrigger(run_date=fire_at),
        args=[ctx.application, user_id, test_text, 0],
        id=f"test_{user_id}_{fire_at.timestamp()}",
        replace_existing=True,
    )
    log.info("Тест запланирован: user=%s", user_id)


# ──────────────────── Обработчик inline-кнопок ──────────────────────

async def callback_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data = query.data  # "del_<id>"

    if not data.startswith("del_"):
        return

    reminder_id = int(data.split("_", 1)[1])

    # Проверяем, что напоминание принадлежит этому пользователю
    row = db_get_reminder_by_id(reminder_id)
    if not row or row["user_id"] != user_id:
        await query.edit_message_text("❌ Напоминание не найдено или уже удалено.")
        return

    deleted = db_delete_reminder(reminder_id, user_id)
    if deleted:
        unschedule_reminder(user_id, reminder_id)
        log.info("Удалено через кнопку: user=%s id=%s", user_id, reminder_id)

    # Обновляем список
    rows = db_get_reminders(user_id)
    text = format_reminder_list(rows)
    if rows:
        keyboard = build_list_keyboard(rows)
        await query.edit_message_text(text, parse_mode="Markdown", reply_markup=keyboard)
    else:
        await query.edit_message_text(text)


# ─────────────────────────── Запуск ─────────────────────────────────

def main() -> None:
    if not BOT_TOKEN:
        log.critical("BOT_TOKEN не задан! Создайте .env файл с BOT_TOKEN=...")
        raise SystemExit(1)

    db_init()

    app = Application.builder().token(BOT_TOKEN).build()

    # Регистрируем обработчики
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CallbackQueryHandler(callback_delete, pattern=r"^del_\d+$"))

    # Загружаем напоминания из БД и запускаем планировщик
    load_all_reminders(app)
    scheduler.start()
    log.info("Планировщик запущен. Временная зона: %s", scheduler.timezone)

    log.info("Бот запускается...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
