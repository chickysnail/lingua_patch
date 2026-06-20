"""lingua_patch — a Telegram bot that sends daily AI-generated language
"patches": a voice note with transcript, translation, and a vocabulary
breakdown of the words that differ most from the learner's mother tongue.

The content pool grows on demand: when a user's unseen patches fall below a
threshold, new items are generated in the background (OpenAI text + ElevenLabs
TTS). Users never receive the same patch twice.
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
from tts import NoNativeVoiceError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("lingua_patch")

router = Router()


# --------------------------------------------------------------------------- #
# Pool expansion (background)
# --------------------------------------------------------------------------- #
_expanding: set[str] = set()  # per-language lock


async def _expand_pool(
    bot: Bot, language: str, native: str, count: int, *, user_id: int = 0,
) -> int:
    """Generate ``count`` new patches for ``language`` in the background.

    De-duped per language so concurrent triggers don't fire duplicate jobs.
    Notifies the admin on completion.
    """
    if language in _expanding:
        return 0
    _expanding.add(language)
    try:
        from generate_content import seed
        added = await asyncio.to_thread(seed, language, native, count)
        log.info("Pool expanded: +%d items for %s (pool now %d).", added, language, db.count_content(language))
        if settings.admin_id and added > 0:
            try:
                await bot.send_message(
                    settings.admin_id,
                    f"🔄 Pool expanded: <b>+{added}</b> patches for <code>{language}</code> "
                    f"(total: {db.count_content(language)})",
                )
            except Exception:  # noqa: BLE001
                log.warning("Failed to notify admin about pool expansion.")
        return added
    except NoNativeVoiceError:
        lang_label = LANGUAGES[language].name if language in LANGUAGES else language
        log.warning("No native voices for %s — notifying user and admin.", language)
        if user_id:
            try:
                await bot.send_message(
                    user_id,
                    f"⚠️ Sorry, native-speaker voices for <b>{lang_label}</b> "
                    "are not available yet. We'll notify you when they are added!",
                )
            except Exception:  # noqa: BLE001
                log.warning("Failed to notify user %d about missing voices.", user_id)
        if settings.admin_id:
            try:
                await bot.send_message(
                    settings.admin_id,
                    f"🔇 No native voices for <code>{language}</code> ({lang_label}). "
                    f"Requested by user <code>{user_id}</code>. "
                    "Please add voices to <code>NATIVE_VOICES</code> in tts.py.",
                )
            except Exception:  # noqa: BLE001
                log.warning("Failed to notify admin about missing voices for %s.", language)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.warning("Pool expansion failed for %s: %s", language, exc)
        return 0
    finally:
        _expanding.discard(language)


def _maybe_expand(bot: Bot, user_id: int, language: str, native: str) -> None:
    """Trigger background pool expansion if the user is running low on unseen patches."""
    unseen = db.count_unsent(user_id, language)
    if unseen <= settings.topup_threshold:
        asyncio.create_task(
            _expand_pool(bot, language, native, settings.topup_count, user_id=user_id),
        )


# --------------------------------------------------------------------------- #
# Core delivery
# --------------------------------------------------------------------------- #
async def deliver(bot: Bot, user: dict[str, Any]) -> bool:
    """Send one patch to a user. Returns True if delivered.

    Users never receive the same patch twice. If no unseen content is available,
    returns False (the caller should ensure pool expansion is triggered).
    """
    user_id = user["user_id"]
    language = user["language"]
    native = user.get("native_language", settings.native_language)

    content = db.pick_unsent_content(user_id, language)
    if content is None:
        log.info("No unseen content for user %s (language=%s).", user_id, language)
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
    _maybe_expand(bot, user_id, language, native)
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
LAST_DAILY_KEY = "last_daily_date"


def pick_next_run(now: datetime, *, force_tomorrow: bool = False) -> datetime:
    """Pick the next random delivery time inside the daytime window.

    Choose a random time in tomorrow's window when ``force_tomorrow`` is set (or
    today's window has already closed); otherwise choose a random time later
    today. A small ``+1`` minute floor avoids scheduling in the past on restarts.
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
    if not force_tomorrow and now < todays_end:
        return random_time_on(now, earliest=now + timedelta(minutes=1))
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=tz)
    return random_time_on(tomorrow, earliest=None)


def _sent_today(now: datetime) -> bool:
    """Whether the daily patch already went out on ``now``'s calendar day."""
    return db.get_meta(LAST_DAILY_KEY) == now.date().isoformat()


def schedule_next(scheduler: AsyncIOScheduler, bot: Bot) -> datetime:
    tz = ZoneInfo(settings.timezone)
    now = datetime.now(tz)
    run_at = pick_next_run(now, force_tomorrow=_sent_today(now))
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
    tz = ZoneInfo(settings.timezone)
    today = datetime.now(tz).date().isoformat()
    try:
        if db.get_meta(LAST_DAILY_KEY) == today:
            log.info("Daily patch already sent on %s; skipping this run.", today)
            return
        db.set_meta(LAST_DAILY_KEY, today)
        await deliver_to_all(bot)
    finally:
        schedule_next(scheduler, bot)


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
PATCH_NOW_TEXT = "GET MORE"


def _patch_keyboard() -> ReplyKeyboardMarkup:
    """Persistent reply keyboard with a single 'GET MORE' button."""
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=PATCH_NOW_TEXT)]],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Tap GET MORE for a patch",
    )


async def send_patch_now(message: Message, bot: Bot) -> None:
    """On-demand delivery: anyone can ask for a patch right now."""
    db.upsert_user(message.from_user.id)
    user = db.get_user(message.from_user.id)
    native = user.get("native_language", settings.native_language)
    delivered = await deliver(bot, user)
    if not delivered:
        _maybe_expand(bot, message.from_user.id, user["language"], native)
        await message.answer(
            "Готую нові патчі для цієї мови — спробуй ще раз за хвилину 🙏"
        )


def _switch_message(code: str) -> str:
    lang = get(code)
    base = f"Готово! Тепер ти вчиш: {lang.flag} <b>{lang.name}</b>."
    if db.count_content(code) == 0:
        base += "\n\nГотую перші патчі для цієї мови — це займе до хвилини. Потім тисни GET MORE."
    return base


def _language_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(text=f"{lang.flag} {lang.name}", callback_data=f"setlang:{code}")
        for code, lang in LANGUAGES.items()
    ]
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("start"))
async def cmd_start(message: Message, bot: Bot) -> None:
    db.upsert_user(message.from_user.id)
    user = db.get_user(message.from_user.id)
    lang = get(user["language"]) if is_supported(user["language"]) else None
    current = f"{lang.flag} {lang.name}" if lang else user["language"]
    native = user.get("native_language", settings.native_language)
    _maybe_expand(bot, message.from_user.id, user["language"], native)
    await message.answer(
        "👋 Привіт! Я надсилатиму тобі <b>один аудіо-патч на день</b> — "
        "текст, переклад і кілька слів, що найбільше відрізняються від рідної "
        f"мови.\n\nЗараз ти вчиш: <b>{current}</b>.\n\n"
        "Кнопка <b>GET MORE</b> — отримай патч прямо зараз\n\n"
        "Команди:\n"
        "• /patch — хочу патч 🎧\n"
        "• /language — змінити мову\n"
        "• раз на день у випадковий час (як BeReal) сам прийде новий патч",
        reply_markup=_patch_keyboard(),
    )


@router.message(Command("language"))
async def cmd_language(message: Message, command: CommandObject, bot: Bot) -> None:
    db.upsert_user(message.from_user.id)
    arg = (command.args or "").strip().lower()
    if arg:
        if not is_supported(arg):
            supported = ", ".join(LANGUAGES.keys())
            await message.answer(f"Не знаю мову <code>{arg}</code>. Доступні: {supported}")
            return
        db.set_user_language(message.from_user.id, arg)
        await message.answer(_switch_message(arg))
        _maybe_expand(bot, message.from_user.id, arg, settings.native_language)
        return
    await message.answer("Обери мову, яку хочеш вчити:", reply_markup=_language_keyboard())


@router.callback_query(F.data.startswith("setlang:"))
async def on_set_language(callback: CallbackQuery, bot: Bot) -> None:
    code = callback.data.split(":", 1)[1]
    if not is_supported(code):
        await callback.answer("Невідома мова", show_alert=True)
        return
    db.upsert_user(callback.from_user.id)
    db.set_user_language(callback.from_user.id, code)
    await callback.message.edit_text(_switch_message(code))
    _maybe_expand(bot, callback.from_user.id, code, settings.native_language)
    await callback.answer()


@router.message(Command("patch"))
async def cmd_patch(message: Message, bot: Bot) -> None:
    await send_patch_now(message, bot)


@router.message(F.text == PATCH_NOW_TEXT)
async def on_patch_button(message: Message, bot: Bot) -> None:
    await send_patch_now(message, bot)


async def setup_commands(bot: Bot) -> None:
    """Populate the public Telegram command menu."""
    await bot.set_my_commands(
        [
            BotCommand(command="patch", description="Хочу патч 🎧"),
            BotCommand(command="language", description="Змінити мову, яку вчиш"),
            BotCommand(command="start", description="Почати / показати поточну мову"),
        ]
    )


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #
async def maybe_seed_on_start(bot: Bot) -> None:
    """Top up the content pool on boot for languages listed in SEED_ON_START.

    Keeps a fresh deploy usable without a manual seeding step.
    """
    codes = [c.strip() for c in settings.seed_on_start.split(",") if c.strip()]
    if not codes:
        return
    for code in codes:
        have = db.count_content(code)
        if have >= settings.seed_count:
            log.info("Seed-on-start: %s already has %d items, skipping.", code, have)
            continue
        need = settings.seed_count - have
        log.info("Seed-on-start: topping up %s (have %d, want %d)...", code, have, settings.seed_count)
        await _expand_pool(bot, code, settings.native_language, need)


async def main() -> None:
    if not settings.bot_token:
        raise SystemExit("BOT_TOKEN is not set. Add it to the environment or .env file.")

    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    db.init_db()
    bot = Bot(token=settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    await maybe_seed_on_start(bot)
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
