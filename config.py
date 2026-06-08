"""Configuration for lingua_patch loaded from environment variables / .env."""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Secrets
    bot_token: str = ""
    openai_api_key: str = ""
    elevenlabs_api_key: str = ""

    # Audio source for patches: "elevenlabs" (AI voice, any text) or "tatoeba"
    # (real native-speaker recordings, limited pool). YouGlish word links are added
    # regardless of source.
    audio_source: str = "elevenlabs"

    # ElevenLabs settings. eleven_multilingual_v2 = best quality (1 credit/char);
    # eleven_flash_v2_5 = ~half the cost. Leave voice ids empty to use the curated
    # multilingual pool (a random voice is picked per clip for variety).
    elevenlabs_model: str = "eleven_multilingual_v2"
    elevenlabs_voice_ids: str = ""

    # On boot, cap how many 'tatoeba'-source items to keep per seeded language
    # (deleting the excess + their audio). 0 = keep all. Useful when switching the
    # audio source but wanting to retain a few real native recordings.
    keep_tatoeba: int = 0

    # Two on-demand patch styles. "short" = a single native sentence (Tatoeba);
    # "long" = a 2-4 sentence AI-voiced snippet (ElevenLabs). The daily auto-send
    # uses the long one.
    short_source: str = "tatoeba"
    long_source: str = "elevenlabs"
    # When a user's unsent items for a given source fall below this, the bot tops
    # the pool up in the background so the buttons never go dry.
    topup_threshold: int = 3
    topup_count: int = 5

    # Admin who may use /test_send (numeric Telegram user id). 0 disables the command.
    admin_id: int = 0

    # OpenAI model used only to extract the vocabulary that differs from the native language.
    openai_model: str = "gpt-4o-mini"

    # Daily delivery schedule. The exact moment is randomised each day
    # (BeReal-style) inside the [send_window_start_hour, send_window_end_hour)
    # window in the configured timezone.
    timezone: str = "Europe/Kyiv"
    send_window_start_hour: int = 9
    send_window_end_hour: int = 21

    # Default learning setup for new users.
    default_language: str = "ukr"  # target language the user is learning
    native_language: str = "rus"   # the user's mother tongue (used for translations + diffing)

    # Paths. Point these at a persistent volume in production (e.g. /data on Railway).
    db_path: Path = BASE_DIR / "bot.db"
    media_dir: Path = BASE_DIR / "media"

    # Optional auto-seed on startup: comma-separated target codes (e.g. "ukr,spa").
    # On boot, any listed language whose pool has fewer than seed_count items is
    # topped up from Tatoeba. Leave empty to disable (seed manually instead).
    seed_on_start: str = ""
    seed_count: int = 10


settings = Settings()
settings.media_dir.mkdir(parents=True, exist_ok=True)
