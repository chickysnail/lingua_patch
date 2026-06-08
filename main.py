"""lingua_patch — a Telegram bot that sends one short native-audio language
"patch" per day: a real native-speaker recording (Tatoeba) with transcript, a
native-language translation, and a small vocabulary breakdown of the words that
differ most from the learner's mother tongue.
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

import db
from config import settings
from formatting import build_message
from languages import LANGUAGES, get, is_supported

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("lingua_patch")

router = Router()


# --------------------------------------------------------------------------- #
# Core delivery
# --------------------------------------------------------------------------- #
async def deliver(bot: Bot, user: dict[str, Any]) -> bool:
    """Send one daily patch to a user. Returns True if delivered.

    On TelegramForbiddenError (user blocked the bot) the user is deactivated so
    we stop trying. Any content with a missing audio file is skipped safely.
    """
    user_id = user["user_id"]
    language = user["language"]
    content = db.pick_unsent_content(user_id, language)
    if content is None:
        log.info("No content available for user %s (language=%s).", user_id, language)
        return False

    audio_path = Path(content["audio_path"])
    if not audio_path.exists():
        log.error("Audio file missing for content id=%s: %s", content["id"], audio_path)
        return False

    try:
        await bot.send_voice(user_id, voice=FSInputFile(audio_path))
        await bot.send_message(user_id, build_message(content), disable_web_page_preview=True)
    except TelegramForbiddenError:
        log.info("User %s blocked the bot — deactivating.", user_id)
        db.set_user_active(user_id, False)
        return False
    except Exception as exc:  # noqa: BLE001
        log.exception("Failed to deliver to %s: %s", user_id, exc)
        return False

    db.record_sent(user_id, content["id"])
    return True


async def deliver_to_all(bot: Bot) -> int:
    users = db.get_active_users()
    log.info("Daily run: delivering to %d active user(s).", len(users))
    sent = 0
    for user in users:
        if await deliver(bot, user):
            sent += 1
    log.info("Daily run complete: %d delivered.", sent)
    return sent


# --------------------------------------------------------------------------- #
# BeReal-style random daily scheduling
# --------------------------------------------------------------------------- #
JOB_ID = "daily_patch"


def pick_next_run(now: datetime) -> datetime:
    """Pick the next random delivery time inside the daytime window.

    If today's window has not yet closed, choose a random time later today;
    otherwise choose a random time tomorrow. A small ``+1`` minute floor avoids
    scheduling in the past on restarts.
    """
    start_h = settings.send_window_start_hour
    end_h = settings.send_window_end_hour
    tz = now.tzinfo

    def random_time_on(day: datetime, earliest: datetime | None) -> datetime:
        window_start = day.replace(hour=start_h, minute=0, second=0, microsecond=0)
        window_end = day.replace(hour=end_h, minute=0, second=0, microsecond=0)
        lower = max(window_start, earliest) if earliest else window_start
        span = int((window_end - lower).total_seconds())
        offset = random.randint(0, span) if span > 0 else 0
        return lower + timedelta(seconds=offset)

    todays_end = now.replace(hour=end_h, minute=0, second=0, microsecond=0)
    if now < todays_end:
        return random_time_on(now, earliest=now + timedelta(minutes=1))
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
    return random_time_on(tomorrow, earliest=None)


def schedule_next(scheduler: AsyncIOScheduler, bot: Bot) -> datetime:
    tz = ZoneInfo(settings.timezone)
    run_at = pick_next_run(datetime.now(tz))
    scheduler.add_job(
        send_and_reschedule,
        trigger=DateTrigger(run_date=run_at),
        args=[scheduler, bot],
        id=JOB_ID,
        replace_existing=True,
    )
    log.info("Next patch scheduled for %s (%s).", run_at.isoformat(), settings.timezone)
    return run_at


async def send_and_reschedule(scheduler: AsyncIOScheduler, bot: Bot) -> None:
    try:
        await deliver_to_all(bot)
    finally:
        # Always line up tomorrow's random time, even if today's send failed.
        schedule_next(scheduler, bot)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
PATCH_NOW_TEXT = "🎧 Хочу патч зараз"


def _patch_keyboard() -> ReplyKeyboardMarkup:
    """Persistent reply keyboard giving every user a one-tap 'patch me now' button."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=PATCH_NOW_TEXT)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Натисни 🎧 Хочу патч зараз",
    )


async def send_patch_now(message: Message, bot: Bot) -> None:
    """On-demand delivery: anyone can ask for a patch right now (button or /patch)."""
    db.upsert_user(message.from_user.id)
    delivered = await deliver(bot, db.get_user(message.from_user.id))
    if not delivered:
        await message.answer(
            "Поки що немає контенту для цієї мови 😔 Спробуй іншу через /language."
        )


def _language_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=f"{lang.flag} {lang.name}", callback_data=f"setlang:{code}")
        for code, lang in LANGUAGES.items()
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("start"))
async def cmd_start(message: Message) -> None:
    db.upsert_user(message.from_user.id)
    user = db.get_user(message.from_user.id)
    lang = get(user["language"]) if is_supported(user["language"]) else None
    current = f"{lang.flag} {lang.name}" if lang else user["language"]
    await message.answer(
        "👋 Привіт! Я надсилатиму тобі <b>один короткий аудіо-патч на день</b> — "
        "реальний голос носія, текст, переклад і кілька слів, що найбільше "
        f"відрізняються від рідної мови.\n\nЗараз ти вчиш: <b>{current}</b>.\n\n"
        "Команди:\n"
        "• /patch — хочу слова прямо зараз 🎧\n"
        "• /language — змінити мову\n"
        "• раз на день у випадковий час (як BeReal) сам прийде новий патч",
        reply_markup=_patch_keyboard(),
    )


@router.message(Command("language"))
async def cmd_language(message: Message, command: CommandObject) -> None:
    db.upsert_user(message.from_user.id)
    arg = (command.args or "").strip().lower()
    if arg:
        if not is_supported(arg):
            supported = ", ".join(LANGUAGES.keys())
            await message.answer(f"Не знаю мову <code>{arg}</code>. Доступні: {supported}")
            return
        db.set_user_language(message.from_user.id, arg)
        lang = get(arg)
        await message.answer(f"Готово! Тепер ти вчиш: {lang.flag} <b>{lang.name}</b>.")
        return
    await message.answer("Обери мову, яку хочеш вчити:", reply_markup=_language_keyboard())


@router.callback_query(F.data.startswith("setlang:"))
async def on_set_language(callback: CallbackQuery) -> None:
    code = callback.data.split(":", 1)[1]
    if not is_supported(code):
        await callback.answer("Невідома мова", show_alert=True)
        return
    db.upsert_user(callback.from_user.id)
    db.set_user_language(callback.from_user.id, code)
    lang = get(code)
    await callback.message.edit_text(f"Готово! Тепер ти вчиш: {lang.flag} <b>{lang.name}</b>.")
    await callback.answer()


@router.message(Command("patch", "test_send"))
async def cmd_patch(message: Message, bot: Bot) -> None:
    await send_patch_now(message, bot)


@router.message(F.text == PATCH_NOW_TEXT)
async def on_patch_now_button(message: Message, bot: Bot) -> None:
    await send_patch_now(message, bot)


async def setup_commands(bot: Bot) -> None:
    """Populate the public Telegram command menu."""
    await bot.set_my_commands(
        [
            BotCommand(command="patch", description="Хочу патч зараз 🎧"),
            BotCommand(command="language", description="Змінити мову, яку вчиш"),
            BotCommand(command="start", description="Почати / показати поточну мову"),
        ]
    )


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #
async def maybe_seed_on_start() -> None:
    """Top up the content pool on boot for languages listed in SEED_ON_START.

    Keeps a fresh deploy (e.g. an empty Railway volume) usable without a manual
    seeding step. Runs the synchronous seeder off the event loop.
    """
    codes = [c.strip() for c in settings.seed_on_start.split(",") if c.strip()]
    if not codes:
        return
    from generate_content import seed  # local import to avoid a heavy import at module load

    for code in codes:
        have = db.count_content(code)
        if have >= settings.seed_count:
            log.info("Seed-on-start: %s already has %d items, skipping.", code, have)
            continue
        log.info("Seed-on-start: topping up %s (have %d, want %d)...", code, have, settings.seed_count)
        try:
            await asyncio.to_thread(seed, code, settings.native_language, settings.seed_count)
        except Exception as exc:  # noqa: BLE001
            log.exception("Seed-on-start failed for %s: %s", code, exc)


async def main() -> None:
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is not set. Add it to the environment or .env file.")

    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    db.init_db()
    await maybe_seed_on_start()
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await setup_commands(bot)

    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    scheduler.start()
    run_at = schedule_next(scheduler, bot)
    log.info(
        "Scheduler started: one random patch/day in [%02d:00, %02d:00) %s. "
        "Next: %s. Pool size: %d.",
        settings.send_window_start_hour, settings.send_window_end_hour,
        settings.timezone, run_at.isoformat(), db.count_content(),
    )

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
