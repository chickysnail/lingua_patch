"""Supported languages and their display / YouGlish metadata.

Keys are ISO 639-3 codes. ``youglish`` is the slug YouGlish uses in its
pronounce URLs: ``https://youglish.com/pronounce/<word>/<youglish-slug>``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str          # ISO 639-3
    name: str          # human readable, shown to the user
    flag: str          # emoji flag for the message header
    youglish: str      # slug for youglish.com pronounce URLs


LANGUAGES: dict[str, Language] = {
    # Slavic focus first.
    "ukr": Language("ukr", "Українська", "🇺🇦", "ukrainian"),
    "bul": Language("bul", "Български", "🇧🇬", "bulgarian"),
    "slk": Language("slk", "Slovenčina", "🇸🇰", "slovak"),
    # Wider set.
    "eng": Language("eng", "English", "🇬🇧", "english"),
    "deu": Language("deu", "Deutsch", "🇩🇪", "german"),
    "por": Language("por", "Português (Brasil)", "🇧🇷", "portuguese"),
}

# English names used in OpenAI prompts (clearer for the model than native script).
ENGLISH_NAMES = {
    "ukr": "Ukrainian", "bul": "Bulgarian", "slk": "Slovak",
    "eng": "English", "deu": "German",
    "por": "Brazilian Portuguese", "rus": "Russian",
}

# Native languages we know how to talk about (used for prompts / labels).
NATIVE_NAMES = {
    "rus": "Russian",
    "eng": "English",
    "ukr": "Ukrainian",
}

# ISO 639-3 -> ISO 639-1, the code ElevenLabs flash/turbo models accept to pin the
# spoken language (multilingual_v2 auto-detects, so this is best-effort).
ISO_639_1 = {
    "ukr": "uk", "rus": "ru", "bul": "bg", "slk": "sk",
    "eng": "en", "deu": "de", "por": "pt",
}


def is_supported(code: str) -> bool:
    return code in LANGUAGES


def get(code: str) -> Language:
    return LANGUAGES[code]
