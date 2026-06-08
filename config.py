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

    # Admin who may use /test_send (numeric Telegram user id). 0 disables the command.
    admin_id: int = 0

    # OpenAI model used only to extract the vocabulary that differs from the native language.
    openai_model: str = "gpt-4o-mini"

    # Daily delivery schedule.
    timezone: str = "Europe/Kyiv"
    daily_hour: int = 10
    daily_minute: int = 0

    # Default learning setup for new users.
    default_language: str = "ukr"  # target language the user is learning
    native_language: str = "rus"   # the user's mother tongue (used for translations + diffing)

    # Paths.
    db_path: Path = BASE_DIR / "bot.db"
    media_dir: Path = BASE_DIR / "media"


settings = Settings()
settings.media_dir.mkdir(parents=True, exist_ok=True)
