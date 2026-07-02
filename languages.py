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
    "pol": Language("pol", "Polski", "🇵🇱", "polish"),
    "ces": Language("ces", "Čeština", "🇨🇿", "czech"),
    # Wider set.
    "eng": Language("eng", "English", "🇬🇧", "english"),
    "deu": Language("deu", "Deutsch", "🇩🇪", "german"),
    "fra": Language("fra", "Français", "🇫🇷", "french"),
    "spa": Language("spa", "Español", "🇪🇸", "spanish"),
    "ita": Language("ita", "Italiano", "🇮🇹", "italian"),
    "por": Language("por", "Português (Brasil)", "🇧🇷", "portuguese"),
    "tur": Language("tur", "Türkçe", "🇹🇷", "turkish"),
    # East Asian.
    "jpn": Language("jpn", "日本語", "🇯🇵", "japanese"),
    "kor": Language("kor", "한국어", "🇰🇷", "korean"),
    "zho": Language("zho", "中文", "🇨🇳", "chinese"),
}

# English names used in OpenAI prompts (clearer for the model than native script).
ENGLISH_NAMES = {
    "ukr": "Ukrainian", "bul": "Bulgarian", "slk": "Slovak",
    "pol": "Polish", "ces": "Czech",
    "eng": "English", "deu": "German", "fra": "French",
    "spa": "Spanish", "ita": "Italian",
    "por": "Brazilian Portuguese", "tur": "Turkish",
    "jpn": "Japanese", "kor": "Korean", "zho": "Mandarin Chinese",
    "rus": "Russian",
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
    "pol": "pl", "ces": "cs",
    "eng": "en", "deu": "de", "fra": "fr",
    "spa": "es", "ita": "it", "por": "pt", "tur": "tr",
    "jpn": "ja", "kor": "ko", "zho": "zh",
}


def is_supported(code: str) -> bool:
    return code in LANGUAGES


def get(code: str) -> Language:
    return LANGUAGES[code]
