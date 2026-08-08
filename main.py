"""lingua_patch — a Telegram bot that sends daily AI-generated language
"patches": a voice note with transcript, translation, and a vocabulary
breakdown of the words that differ most from the learner's mother tongue.

The content pool grows on demand: when a user's unseen patches fall below a
threshold, new items are generated in the background (OpenAI text + ElevenLabs
TTS). Users never receive the same patch twice.
"""
from __future__ import annotations

import asyncio
import html
import logging
import random
import tempfile
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

import db
import speaking
from config import settings
from formatting import build_message
from languages import LANGUAGES, get, is_supported
from tts import NoNativeVoiceError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("lingua_patch")

router = Router()

# Set once in main(); used by /time handlers to (un)schedule per-user jobs.
_scheduler: AsyncIOScheduler | None = None

# In-memory state for the current speaking exercise per user. No DB persistence.
_active_exercises: dict[int, dict[str, Any]] = {}


# --------------------------------------------------------------------------- #
# Pool expansion (background)
# --------------------------------------------------------------------------- #
_expanding: set[str] = set()  # per-(language, difficulty) lock


async def _expand_pool(
    bot: Bot, language: str, native: str, count: int, *,
    user_id: int = 0, difficulty: str | None = None,
) -> int:
    """Generate ``count`` new patches for ``language``/``difficulty`` in the background.

    De-duped per (language, difficulty) so concurrent triggers don't fire
    duplicate jobs. Notifies the admin on completion.
    """
    key = f"{language}:{difficulty or ''}"
    if key in _expanding:
        return 0
    _expanding.add(key)
    try:
        from generate_content import seed
        added = await asyncio.to_thread(seed, language, native, count, difficulty=difficulty)
        pool_now = db.count_content(language, difficulty)
        log.info("Pool expanded: +%d items for %s (tier=%s, pool now %d).",
                 added, language, difficulty or "default", pool_now)
        if settings.admin_id and added > 0:
            try:
                tier = f" [{difficulty}]" if difficulty else ""
                await bot.send_message(
                    settings.admin_id,
                    f"🔄 Pool expanded: <b>+{added}</b> patches for <code>{language}</code>{tier} "
                    f"(total: {pool_now})",
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
        _expanding.discard(key)


def _maybe_expand(
    bot: Bot, user_id: int, language: str, native: str, difficulty: str | None = None
) -> None:
    """Trigger background pool expansion if the user is running low on unseen patches."""
    unseen = db.count_unsent(user_id, language, difficulty)
    if unseen <= settings.topup_threshold:
        asyncio.create_task(
            _expand_pool(
                bot, language, native, settings.topup_count,
                user_id=user_id, difficulty=difficulty,
            ),
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
    difficulty = user.get("difficulty")

    content = db.pick_unsent_content(user_id, language, difficulty)
    if content is None:
        log.info("No unseen content for user %s (language=%s, tier=%s).",
                 user_id, language, difficulty or "default")
        return False

    audio_path = Path(content["audio_path"])
    if not audio_path.exists():
        log.error("Audio file missing for content id=%s: %s", content["id"], audio_path)
        return False

    try:
        await bot.send_voice(user_id, voice=FSInputFile(audio_path))
        practice_button = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text=LET_S_PRACTICE, callback_data=f"{PRACTICE_CALLBACK}:{content['id']}")]
            ]
        )
        await bot.send_message(
            user_id,
            build_message(content),
            reply_markup=practice_button,
            disable_web_page_preview=True,
        )
    except TelegramForbiddenError:
        log.info("User %s blocked the bot — deactivating.", user_id)
        db.set_user_active(user_id, False)
        return False
    except Exception:
        log.exception("Failed to deliver to %s", user_id)
        return False

    db.record_sent(user_id, content["id"])
    _maybe_expand(bot, user_id, language, native, difficulty)
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


def schedule_user_job(
    scheduler: AsyncIOScheduler, bot: Bot, user_id: int, send_time: str
) -> None:
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
PRACTICE_TEXT = "Practice"


LET_S_PRACTICE = "Let's practice"
PRACTICE_CALLBACK = "practice"

# Difficulty tiers. None (the default) uses the shared unmarked pool.
DIFFICULTY_LABELS: dict[str, str] = {
    "easy": "🟢 Простой",
    "medium": "🟡 Средний",
    "hard": "🔴 Сложный",
}


def _difficulty_label(difficulty: str | None) -> str:
    return DIFFICULTY_LABELS.get(difficulty or "", "⚪️ Без уровня (обычный пул)")


def _level_keyboard(current: str | None) -> InlineKeyboardMarkup:
    """Inline keyboard to pick a difficulty tier; marks the active one."""
    def label(key: str | None, text: str) -> str:
        return f"✅ {text}" if key == current else text

    rows = [
        [InlineKeyboardButton(text=label("easy", DIFFICULTY_LABELS["easy"]), callback_data="setlevel:easy")],
        [InlineKeyboardButton(text=label("medium", DIFFICULTY_LABELS["medium"]), callback_data="setlevel:medium")],
        [InlineKeyboardButton(text=label("hard", DIFFICULTY_LABELS["hard"]), callback_data="setlevel:hard")],
        [InlineKeyboardButton(text=label(None, "⚪️ Без уровня"), callback_data="setlevel:none")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _patch_keyboard() -> ReplyKeyboardMarkup:
    """Persistent reply keyboard with patch and practice buttons."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=PATCH_NOW_TEXT)],
            [KeyboardButton(text=PRACTICE_TEXT)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Tap GET MORE or Practice",
    )


async def send_patch_now(message: Message, bot: Bot) -> None:
    """On-demand delivery: anyone can ask for a patch right now."""
    db.upsert_user(message.from_user.id)
    user = db.get_user(message.from_user.id)
    native = user.get("native_language", settings.native_language)
    delivered = await deliver(bot, user)
    if not delivered:
        _maybe_expand(bot, message.from_user.id, user["language"], native, user.get("difficulty"))
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
    _maybe_expand(bot, message.from_user.id, user["language"], native, user.get("difficulty"))
    await message.answer(
        "👋 Привет! Я буду присылать тебе <b>один аудио-патч в день</b> — "
        "текст, перевод и несколько слов, которые больше всего отличаются от родного "
        f"языка.\n\nСейчас ты учишь: <b>{current}</b>.\n\n"
        "Кнопка <b>GET MORE</b> — получи патч прямо сейчас\n\n"
        "Команды:\n"
        "• /patch — хочу патч 🎧\n"
        "• /language — сменить язык\n"
        "• /level — уровень сложности (простой/средний/сложный)\n"
        "• /time — настроить время отправки 🕒\n"
        "• по умолчанию патч приходит раз в день в случайное время",
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
        user = db.get_user(message.from_user.id)
        _maybe_expand(bot, message.from_user.id, arg, settings.native_language, user.get("difficulty"))
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
    user = db.get_user(callback.from_user.id)
    _maybe_expand(bot, callback.from_user.id, code, settings.native_language, user.get("difficulty"))
    await callback.answer()


@router.message(Command("level"))
async def cmd_level(message: Message, bot: Bot) -> None:
    db.upsert_user(message.from_user.id)
    user = db.get_user(message.from_user.id)
    current = user.get("difficulty")
    await message.answer(
        "Выбери уровень сложности патчей.\n\n"
        f"Сейчас: <b>{_difficulty_label(current)}</b>\n\n"
        "⚪️ <b>Без уровня</b> — обычный общий пул (как по умолчанию).\n"
        "🟢🟡🔴 — патчи будут подбираться/генерироваться под выбранную сложность.",
        reply_markup=_level_keyboard(current),
    )


@router.callback_query(F.data.startswith("setlevel:"))
async def on_set_level(callback: CallbackQuery, bot: Bot) -> None:
    raw = callback.data.split(":", 1)[1]
    difficulty = None if raw == "none" else raw
    if difficulty is not None and difficulty not in DIFFICULTY_LABELS:
        await callback.answer("Неизвестный уровень", show_alert=True)
        return
    db.upsert_user(callback.from_user.id)
    db.set_user_difficulty(callback.from_user.id, difficulty)
    user = db.get_user(callback.from_user.id)
    native = user.get("native_language", settings.native_language)
    await callback.message.edit_text(
        f"Готово! Уровень: <b>{_difficulty_label(difficulty)}</b>.\n\n"
        + (
            "Патчи будут из обычного общего пула."
            if difficulty is None
            else "Готовлю патчи под этот уровень — жми GET MORE через минуту, "
            "если их ещё нет."
        )
    )
    _maybe_expand(bot, callback.from_user.id, user["language"], native, difficulty)
    await callback.answer()


@router.message(Command("patch"))
async def cmd_patch(message: Message, bot: Bot) -> None:
    await send_patch_now(message, bot)


@router.message(F.text == PATCH_NOW_TEXT)
async def on_patch_button(message: Message, bot: Bot) -> None:
    await send_patch_now(message, bot)


# --------------------------------------------------------------------------- #
# Speaking practice
# --------------------------------------------------------------------------- #
async def _start_practice(user_id: int, bot: Bot, context: str | None = None) -> None:
    """Generate and send a speaking exercise."""
    db.upsert_user(user_id)
    user = db.get_user(user_id)
    if not user:
        await bot.send_message(user_id, "Не удалось найти пользователя.")
        return

    language = user["language"]
    native = user.get("native_language", settings.native_language)
    difficulty = user.get("difficulty")

    try:
        exercise = await asyncio.to_thread(
            speaking.generate_exercise, language, native, difficulty, context
        )
    except Exception:
        log.exception("Failed to generate exercise for user %s", user_id)
        await bot.send_message(user_id, "Не удалось придумать упражнение. Попробуй ещё раз позже.")
        return

    if not exercise.get("source_sentence"):
        await bot.send_message(user_id, "Не удалось придумать предложение. Попробуй ещё раз.")
        return

    html_path: Path | None = None
    try:
        html_path = speaking.build_exercise_html(exercise)
        await bot.send_document(
            user_id,
            document=FSInputFile(html_path),
            caption="Открой файл и запиши голосовое сообщение с переводом 🎙",
        )
    except Exception:
        log.exception("Failed to send exercise for user %s", user_id)
        await bot.send_message(user_id, "Не удалось отправить упражнение. Попробуй ещё раз.")
        return
    finally:
        if html_path and html_path.exists():
            html_path.unlink(missing_ok=True)

    _active_exercises[user_id] = {
        "source_sentence": exercise["source_sentence"],
        "language": language,
        "native_language": native,
    }


@router.callback_query(F.data.startswith(f"{PRACTICE_CALLBACK}:"))
async def on_practice_callback(callback: CallbackQuery, bot: Bot) -> None:
    """Inline 'Let's practice' button below a daily patch."""
    content_id = callback.data.split(":", 1)[1]
    try:
        content_id_int = int(content_id)
    except ValueError:
        await callback.answer("Неверное упражнение", show_alert=True)
        return

    content = db.get_content(content_id_int)
    if not content:
        await callback.answer("Патч не найден", show_alert=True)
        return

    await callback.answer()
    await _start_practice(callback.from_user.id, bot, context=content["transcript"])


@router.message(Command("practice"))
async def cmd_practice(message: Message, bot: Bot) -> None:
    """Start a practice exercise from the command menu."""
    await _start_practice(message.from_user.id, bot)


@router.message(F.text == PRACTICE_TEXT)
async def on_practice_button(message: Message, bot: Bot) -> None:
    """Persistent reply-keyboard 'Practice' button."""
    await _start_practice(message.from_user.id, bot)


@router.message(F.voice)
async def on_voice(message: Message, bot: Bot) -> None:
    """Accept a voice answer to the current speaking exercise."""
    user_id = message.from_user.id
    exercise = _active_exercises.get(user_id)
    if not exercise:
        return

    audio_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            await bot.download(message.voice, destination=tmp.name)
            audio_path = Path(tmp.name)
    except Exception:
        log.exception("Failed to download voice from user %s", user_id)
        await message.answer("Не удалось скачать голосовое. Попробуй ещё раз.")
        return
    finally:
        if audio_path and audio_path.exists():
            audio_path.unlink(missing_ok=True)

    text: str | None = None
    try:
        text = await asyncio.to_thread(
            speaking.transcribe_voice, audio_path, exercise["language"]
        )
    except Exception:
        log.exception("STT failed for user %s", user_id)
        await message.answer("Не удалось распознать речь. Попробуй ещё раз.")
        return

    if not text:
        await message.answer("Я ничего не услышал. Попробуй ещё раз.")
        return

    try:
        result = await asyncio.to_thread(
            speaking.evaluate_translation,
            exercise["source_sentence"],
            text,
            exercise["language"],
            exercise["native_language"],
        )
    except Exception:
        log.exception("Evaluation failed for user %s", user_id)
        await message.answer("Не удалось оценить ответ. Попробуй ещё раз.")
        return

    response = f"<b>{html.escape(result['status'])}</b>\n\n{html.escape(result['feedback'])}"
    await message.answer(response)
    _active_exercises.pop(user_id, None)


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


# --------------------------------------------------------------------------- #
# Statistics (/stats)
# --------------------------------------------------------------------------- #
def _language_label(code: str) -> str:
    if is_supported(code):
        lang = get(code)
        return f"{lang.flag} {lang.name}"
    return code


def _time_label(user: dict[str, Any]) -> str:
    send_time = (user.get("send_time") or "").strip()
    return f"🕒 {send_time}" if send_time else "🎲 случайное"


def _short_date(iso: str | None) -> str:
    """'2026-07-28T21:12:00+00:00' -> '2026-07-28'; '—' when missing."""
    return iso.split("T")[0] if iso else "—"


async def _display_name(bot: Bot, user_id: int) -> str:
    """Resolve a user's @username (or full name) via Telegram; fall back to the id."""
    try:
        chat = await bot.get_chat(user_id)
    except Exception:  # noqa: BLE001
        return f"id {user_id}"
    if chat.username:
        return f"@{chat.username}"
    name = " ".join(p for p in (chat.first_name, chat.last_name) if p)
    return html.escape(name) if name else f"id {user_id}"


@router.message(Command("stats"))
async def cmd_stats(message: Message, bot: Bot) -> None:
    """Admin-only overview of every user: language, level, delivery time, activity."""
    if not settings.admin_id or message.from_user.id != settings.admin_id:
        return

    users = db.get_all_users_with_stats()
    if not users:
        await message.answer("В базе пока нет пользователей.")
        return

    lines = [f"📊 <b>Пользователи ({len(users)})</b> — время в {settings.timezone}"]
    for u in users:
        name = await _display_name(bot, u["user_id"])
        flags = "" if u["is_active"] else " · 🚫 заблокировал"
        lines.append(
            f"\n<b>{name}</b> <code>{u['user_id']}</code>{flags}\n"
            f"{_language_label(u['language'])} · {_difficulty_label(u.get('difficulty'))} · "
            f"{_time_label(u)}\n"
            f"патчей: {u['patches_sent']} · последний: {_short_date(u['last_sent'])} · "
            f"с {_short_date(u.get('join_date'))}"
        )
    for chunk in _chunk_lines(lines):
        await message.answer(chunk)


def _chunk_lines(lines: list[str], limit: int = 3500) -> list[str]:
    """Group lines into messages below Telegram's 4096-character cap."""
    chunks: list[str] = []
    current = ""
    for line in lines:
        if current and len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


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


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, bot: Bot) -> None:
    """Free text: parse a custom delivery time if we're awaiting one."""
    db.upsert_user(message.from_user.id)
    user = db.get_user(message.from_user.id)
    if not user.get("awaiting_time"):
        return
    parsed = _parse_time(message.text)
    if parsed is None:
        await message.answer(
            "Не понял время 🤔 Напиши в формате <b>ЧЧ:ММ</b>, например 08:30."
        )
        return
    reply = _apply_time_choice(bot, message.from_user.id, parsed)
    await message.answer(reply)


async def setup_commands(bot: Bot) -> None:
    """Populate the public Telegram command menu."""
    await bot.set_my_commands(
        [
            BotCommand(command="patch", description="Хочу патч 🎧"),
            BotCommand(command="practice", description="Практика 🎙"),
            BotCommand(command="language", description="Сменить изучаемый язык"),
            BotCommand(command="level", description="Уровень сложности 📶"),
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
    run_at = schedule_next(scheduler, bot)
    # Restore per-user fixed-time jobs for users who picked a time via /time.
    fixed = db.get_users_with_fixed_time()
    for u in fixed:
        schedule_user_job(scheduler, bot, u["user_id"], u["send_time"])
    log.info(
        "Scheduler started: one random patch/day in [%02d:00, %02d:00) %s. "
        "Next: %s. Fixed-time users: %d. Pool size: %d.",
        settings.send_window_start_hour, settings.send_window_end_hour,
        settings.timezone, run_at.isoformat(), len(fixed), db.count_content(),
    )

    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
