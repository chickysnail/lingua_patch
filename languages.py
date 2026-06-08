"""Supported languages and their display / YouGlish metadata.

Keys are ISO 639-3 codes (matching Tatoeba's ``lang`` parameter). ``youglish``
is the slug YouGlish uses in its pronounce URLs:
``https://youglish.com/pronounce/<word>/<youglish-slug>``.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Language:
    code: str          # ISO 639-3, used by Tatoeba
    name: str          # human readable, shown to the user
    flag: str          # emoji flag for the message header
    youglish: str      # slug for youglish.com pronounce URLs


LANGUAGES: dict[str, Language] = {
    "ukr": Language("ukr", "Українська", "🇺🇦", "ukrainian"),
    "spa": Language("spa", "Español", "🇪🇸", "spanish"),
    "fra": Language("fra", "Français", "🇫🇷", "french"),
    "deu": Language("deu", "Deutsch", "🇩🇪", "german"),
    "ita": Language("ita", "Italiano", "🇮🇹", "italian"),
    "por": Language("por", "Português", "🇵🇹", "portuguese"),
    "pol": Language("pol", "Polski", "🇵🇱", "polish"),
    "eng": Language("eng", "English", "🇬🇧", "english"),
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
    "ukr": "uk", "rus": "ru", "spa": "es", "fra": "fr", "deu": "de",
    "ita": "it", "por": "pt", "pol": "pl", "eng": "en",
}


def is_supported(code: str) -> bool:
    return code in LANGUAGES


def get(code: str) -> Language:
    return LANGUAGES[code]
