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
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import content
import db
import tts
from config import settings
from formatting import build_message
from languages import LANGUAGES, get, is_supported
from tts import ElevenLabsError, NoNativeVoiceError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("lingua_patch")

router = Router()

# Set in main(); handlers need it to (un)schedule per-user delivery jobs.
_scheduler: AsyncIOScheduler | None = None


# --------------------------------------------------------------------------- #
# Pool expansion (background)
# --------------------------------------------------------------------------- #
_expanding: set[str] = set()  # per-language lock (shared pool)
_expanding_personal: set[int] = set()  # per-user lock (personalized buffer)


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


async def _notify_no_voices(bot: Bot, user_id: int, language: str) -> None:
    lang_label = LANGUAGES[language].name if language in LANGUAGES else language
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


async def _seed_personal(
    bot: Bot, user_id: int, language: str, native: str, version: int,
    prompt: str | None, count: int,
) -> int:
    """Generate ``count`` personalized patches for a user (blocking work off-loop)."""
    from generate_content import seed

    try:
        return await asyncio.to_thread(
            seed, language, native, count,
            custom_prompt=prompt, owner_user_id=user_id, rules_version=version,
        )
    except NoNativeVoiceError:
        await _notify_no_voices(bot, user_id, language)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.warning("Personalized generation failed for user %d: %s", user_id, exc)
        return 0


async def _expand_personal(
    bot: Bot, user_id: int, language: str, native: str, version: int, prompt: str | None,
) -> int:
    """Background top-up of a user's personalized buffer, de-duped per user."""
    if user_id in _expanding_personal:
        return 0
    _expanding_personal.add(user_id)
    try:
        added = await _seed_personal(
            bot, user_id, language, native, version, prompt, settings.personal_topup_count,
        )
        if added:
            log.info("Personal buffer +%d for user %d (%s).", added, user_id, language)
        return added
    finally:
        _expanding_personal.discard(user_id)


def _maybe_expand(bot: Bot, user_id: int, language: str, native: str) -> None:
    """Trigger background generation when a user is low on unseen patches.

    Personalized users top up their small per-user buffer; everyone else grows
    the shared pool.
    """
    user = db.get_user(user_id)
    if user and db.is_personalized(user):
        version = user["rules_version"]
        unseen = db.count_unsent(user_id, language, version)
        if unseen <= settings.personal_topup_threshold and user_id not in _expanding_personal:
            asyncio.create_task(
                _expand_personal(bot, user_id, language, native, version, user["personal_prompt"]),
            )
        return
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
    version = user["rules_version"] if db.is_personalized(user) else None

    patch = db.pick_unsent_content(user_id, language, version)
    if patch is None and version is not None:
        # Personalized buffer is empty: generate one on the fly (~5-6s). Fast
        # enough that the caller can show a short "preparing" message.
        await _seed_personal(bot, user_id, language, native, version, user["personal_prompt"], 1)
        patch = db.pick_unsent_content(user_id, language, version)
    if patch is None:
        log.info("No unseen content for user %s (language=%s).", user_id, language)
        return False

    audio_path = Path(patch["audio_path"])
    if not audio_path.exists():
        log.error("Audio file missing for content id=%s: %s", patch["id"], audio_path)
        return False

    try:
        await bot.send_voice(user_id, voice=FSInputFile(audio_path))
        await bot.send_message(user_id, build_message(patch), disable_web_page_preview=True)
    except TelegramForbiddenError:
        log.info("User %s blocked the bot — deactivating.", user_id)
        db.set_user_active(user_id, False)
        return False
    except Exception as exc:  # noqa: BLE001
        log.exception("Failed to deliver to %s: %s", user_id, exc)
        return False

    db.record_sent(user_id, patch["id"])
    _maybe_expand(bot, user_id, language, native)
    return True


async def deliver_to_all(bot: Bot) -> int:
    """Deliver the daily patch to users on the randomized window.

    Users with a fixed ``send_time`` are handled by their own cron jobs.
    """
    users = db.get_random_time_users()
    log.info("Daily run: delivering to %d random-window user(s).", len(users))
    sent = 0
    for user in users:
        if await deliver(bot, user):
            sent += 1
    log.info("Daily run complete: %d delivered.", sent)
    return sent


async def deliver_to_user(bot: Bot, user_id: int) -> None:
    """Deliver one patch to a single user (used by per-user fixed-time jobs)."""
    user = db.get_user(user_id)
    if not user or not user["is_active"]:
        return
    await deliver(bot, user)


# --------------------------------------------------------------------------- #
# Per-user fixed-time scheduling
# --------------------------------------------------------------------------- #
def _user_job_id(user_id: int) -> str:
    return f"user_{user_id}"


def schedule_user_job(scheduler: AsyncIOScheduler, bot: Bot, user_id: int, send_time: str) -> None:
    """Schedule (or replace) a daily delivery at ``send_time`` ('HH:MM')."""
    hour, minute = (int(x) for x in send_time.split(":"))
    scheduler.add_job(
        deliver_to_user,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=settings.timezone),
        args=[bot, user_id],
        id=_user_job_id(user_id),
        replace_existing=True,
    )
    log.info("Scheduled daily patch for user %d at %s %s.", user_id, send_time, settings.timezone)


def unschedule_user_job(scheduler: AsyncIOScheduler, user_id: int) -> None:
    """Remove a user's fixed-time job (they fall back to the random window)."""
    try:
        scheduler.remove_job(_user_job_id(user_id))
    except JobLookupError:
        pass


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
    if db.is_personalized(user) and db.count_unsent(
        user["user_id"], user["language"], user["rules_version"]
    ) == 0:
        await message.answer("⏳ Готовлю персональный патч под твои правила — пара секунд…")
    delivered = await deliver(bot, user)
    if not delivered:
        _maybe_expand(bot, message.from_user.id, user["language"], native)
        await message.answer(
            "Готовлю новые патчи для этого языка — попробуй ещё раз через минуту 🙏"
        )


def _switch_message(code: str) -> str:
    lang = get(code)
    base = f"Готово! Теперь ты учишь: {lang.flag} <b>{lang.name}</b>."
    if db.count_content(code) == 0:
        base += "\n\nГотовлю первые патчи для этого языка — это займёт до минуты. Потом жми GET MORE."
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
        "👋 Привет! Я буду присылать тебе <b>один аудио-патч в день</b> — "
        "текст, перевод и несколько слов, которые больше всего отличаются от родного "
        f"языка.\n\nСейчас ты учишь: <b>{current}</b>.\n\n"
        "Кнопка <b>GET MORE</b> — получи патч прямо сейчас\n\n"
        "✨ <b>Персонализация:</b> просто отправь мне текст или голосовое с "
        "пожеланием (тема, стиль, сложность) — и патчи будут подстраиваться под тебя. "
        "Можно менять в любой момент, я каждый раз спрошу подтверждение.\n\n"
        "Команды:\n"
        "• /patch — хочу патч 🎧\n"
        "• /language — сменить язык\n"
        "• /time — настроить время ежедневного патча\n"
        "• раз в день сам придёт новый патч (по умолчанию в случайное время)",
        reply_markup=_patch_keyboard(),
    )


@router.message(Command("language"))
async def cmd_language(message: Message, command: CommandObject, bot: Bot) -> None:
    db.upsert_user(message.from_user.id)
    arg = (command.args or "").strip().lower()
    if arg:
        if not is_supported(arg):
            supported = ", ".join(LANGUAGES.keys())
            await message.answer(f"Не знаю язык <code>{arg}</code>. Доступные: {supported}")
            return
        db.set_user_language(message.from_user.id, arg)
        await message.answer(_switch_message(arg))
        _maybe_expand(bot, message.from_user.id, arg, settings.native_language)
        return
    await message.answer("Выбери язык, который хочешь учить:", reply_markup=_language_keyboard())


@router.callback_query(F.data.startswith("setlang:"))
async def on_set_language(callback: CallbackQuery, bot: Bot) -> None:
    code = callback.data.split(":", 1)[1]
    if not is_supported(code):
        await callback.answer("Неизвестный язык", show_alert=True)
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


# --------------------------------------------------------------------------- #
# Delivery-time settings (/time)
# --------------------------------------------------------------------------- #
TIME_PRESETS: list[tuple[str, str]] = [
    ("🌅 Утро · 08:00", "08:00"),
    ("☀️ День · 13:00", "13:00"),
    ("🌆 Вечер · 19:00", "19:00"),
    ("🌙 Поздний вечер · 22:00", "22:00"),
]


def _time_keyboard() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, callback_data=f"settime:{val}")]
            for label, val in TIME_PRESETS]
    rows.append([InlineKeyboardButton(text="🎲 Случайное время", callback_data="settime:random")])
    rows.append([InlineKeyboardButton(text="✍️ Своё время", callback_data="settime:custom")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _current_time_line(user: dict[str, Any]) -> str:
    send_time = (user.get("send_time") or "").strip()
    if send_time:
        return f"Сейчас патч приходит в <b>{send_time}</b> ({settings.timezone})."
    return "Сейчас патч приходит в <b>случайное время</b> днём."


@router.message(Command("time"))
async def cmd_time(message: Message, bot: Bot) -> None:
    db.upsert_user(message.from_user.id)
    user = db.get_user(message.from_user.id)
    await message.answer(
        "🕒 Когда присылать ежедневный патч?\n\n"
        f"{_current_time_line(user)}\n\n"
        "Выбери вариант или задай своё время. «Случайное» — как по умолчанию, "
        "всегда можно вернуться к нему.",
        reply_markup=_time_keyboard(),
    )


def _apply_time_choice(bot: Bot, user_id: int, value: str) -> str:
    """Persist a time choice and (un)schedule the user's job. Returns a reply."""
    if value == "random":
        db.set_send_time(user_id, None)
        if _scheduler is not None:
            unschedule_user_job(_scheduler, user_id)
        return "🎲 Готово! Патч будет приходить в случайное время днём."
    db.set_send_time(user_id, value)
    if _scheduler is not None:
        schedule_user_job(_scheduler, bot, user_id, value)
    return f"✅ Готово! Патч будет приходить каждый день в <b>{value}</b> ({settings.timezone})."


@router.callback_query(F.data.startswith("settime:"))
async def on_set_time(callback: CallbackQuery, bot: Bot) -> None:
    db.upsert_user(callback.from_user.id)
    value = callback.data.split(":", 1)[1]
    if value == "custom":
        db.set_awaiting_time(callback.from_user.id, True)
        await callback.message.edit_text(
            "✍️ Напиши время в формате <b>ЧЧ:ММ</b> (например, 08:30 или 21:15)."
        )
        await callback.answer()
        return
    reply = _apply_time_choice(bot, callback.from_user.id, value)
    await callback.message.edit_text(reply)
    await callback.answer()


def _parse_time(text: str) -> str | None:
    """Parse 'HH:MM' / 'H.MM' / 'HH MM' into a canonical 'HH:MM', or None."""
    import re

    m = re.match(r"^\s*(\d{1,2})[:.\s](\d{2})\s*$", text)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


# --------------------------------------------------------------------------- #
# Personalization: text / voice instructions -> confirmable profile
# --------------------------------------------------------------------------- #
def _confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Сохранить", callback_data="rule:save"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="rule:cancel"),
        ]]
    )


async def _process_instruction(message: Message, bot: Bot, text: str) -> None:
    """Interpret a personalization instruction and ask the user to confirm it."""
    user_id = message.from_user.id
    db.upsert_user(user_id)
    user = db.get_user(user_id)
    thinking = await message.answer("🤔 Обдумываю…")
    try:
        result = await asyncio.to_thread(
            content.synthesize_profile, user.get("personal_prompt"), text
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Profile synthesis failed for user %d: %s", user_id, exc)
        await thinking.edit_text("Не получилось обработать пожелание 😔 Попробуй ещё раз.")
        return

    profile = result["profile"]
    summary = result["summary"] or "Вот как я понял твоё пожелание."
    db.set_pending_prompt(user_id, profile)

    if profile.strip():
        body = f"{summary}\n\n<b>Твои правила:</b>\n{profile}\n\nСохранить?"
    else:
        body = f"{summary}\n\nПравила будут очищены — вернёмся к общим патчам. Продолжить?"
    await thinking.edit_text(body, reply_markup=_confirm_keyboard())


@router.callback_query(F.data == "rule:save")
async def on_rule_save(callback: CallbackQuery, bot: Bot) -> None:
    user_id = callback.from_user.id
    db.confirm_pending_prompt(user_id)
    # Old-version personalized patches are now stale — drop them and their files.
    for path in db.discard_unsent_personal(user_id):
        Path(path).unlink(missing_ok=True)
    user = db.get_user(user_id)
    native = user.get("native_language", settings.native_language)
    if db.is_personalized(user):
        await callback.message.edit_text(
            "✅ Сохранил! Готовлю свежие патчи под твои правила — "
            "жми <b>GET MORE</b> через пару секунд."
        )
        _maybe_expand(bot, user_id, user["language"], native)
    else:
        await callback.message.edit_text(
            "✅ Готово! Правила очищены — снова будут приходить общие патчи."
        )
    await callback.answer()


@router.callback_query(F.data == "rule:cancel")
async def on_rule_cancel(callback: CallbackQuery, bot: Bot) -> None:
    db.set_pending_prompt(callback.from_user.id, None)
    await callback.message.edit_text("Ок, ничего не меняю 👌")
    await callback.answer()


@router.message(F.voice)
async def on_voice(message: Message, bot: Bot) -> None:
    """A voice message is treated as a personalization instruction."""
    db.upsert_user(message.from_user.id)
    stamp = int(datetime.now().timestamp() * 1000)
    ogg_path = settings.media_dir / f"rule_{message.from_user.id}_{stamp}.ogg"
    try:
        await bot.download(message.voice, destination=ogg_path)
        text = await asyncio.to_thread(tts.transcribe, ogg_path)
    except ElevenLabsError as exc:
        log.warning("Voice transcription failed for user %d: %s", message.from_user.id, exc)
        await message.answer("Не удалось распознать голосовое 😔 Попробуй ещё раз или напиши текстом.")
        return
    finally:
        ogg_path.unlink(missing_ok=True)
    await _process_instruction(message, bot, text)


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, bot: Bot) -> None:
    """Free-text: a custom delivery time (if awaited) or a personalization rule."""
    db.upsert_user(message.from_user.id)
    user = db.get_user(message.from_user.id)
    if user.get("awaiting_time"):
        parsed = _parse_time(message.text)
        if parsed is None:
            await message.answer("Не понял время 🤔 Напиши в формате <b>ЧЧ:ММ</b>, например 08:30.")
            return
        reply = _apply_time_choice(bot, message.from_user.id, parsed)
        await message.answer(reply)
        return
    await _process_instruction(message, bot, message.text)


async def setup_commands(bot: Bot) -> None:
    """Populate the public Telegram command menu."""
    await bot.set_my_commands(
        [
            BotCommand(command="patch", description="Хочу патч 🎧"),
            BotCommand(command="language", description="Сменить изучаемый язык"),
            BotCommand(command="time", description="Настроить время патча 🕒"),
            BotCommand(command="start", description="Начать / показать текущий язык"),
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

    global _scheduler
    scheduler = AsyncIOScheduler(timezone=settings.timezone)
    _scheduler = scheduler
    scheduler.start()
    for u in db.get_users_with_fixed_time():
        schedule_user_job(scheduler, bot, u["user_id"], u["send_time"])
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
