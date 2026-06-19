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

    # ElevenLabs settings. eleven_multilingual_v2 = best quality.
    elevenlabs_model: str = "eleven_multilingual_v2"
    elevenlabs_voice_ids: str = ""

    # Admin who receives pool-expansion notifications (numeric Telegram user id).
    # 0 disables admin messages.
    admin_id: int = 0

    # OpenAI model for text generation and vocabulary extraction.
    openai_model: str = "gpt-4o-mini"

    # Pool auto-expansion: when a user has <= topup_threshold unseen patches for
    # their language, a background job generates topup_count new items.
    topup_threshold: int = 5
    topup_count: int = 10

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
    # topped up via AI generation. Leave empty to disable.
    seed_on_start: str = ""
    seed_count: int = 10


settings = Settings()
settings.media_dir.mkdir(parents=True, exist_ok=True)
