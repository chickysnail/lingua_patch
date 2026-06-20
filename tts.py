"""ElevenLabs text-to-speech: turn generated text into a voice-note mp3.

Uses the REST API directly (httpx) so there is no extra SDK dependency. The API
key only needs the ``text_to_speech`` permission — voices come from native-speaker
pools in the ElevenLabs Voice Library, so ``voices_read`` is not required.
A random voice is picked per clip for variety.
"""
from __future__ import annotations

import logging
import random
from pathlib import Path

import httpx

from config import settings
from languages import ISO_639_1

log = logging.getLogger(__name__)

ELEVENLABS_TTS = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

# Native-speaker voices from the ElevenLabs Voice Library, keyed by ISO 639-3.
# Only languages with a pool here can generate content; missing languages trigger
# a user notification + admin alert.
NATIVE_VOICES: dict[str, list[tuple[str, str]]] = {
    "eng": [
        ("Rachel", "21m00Tcm4TlvDq8ikWAM"),
        ("Bella", "EXAVITQu4vr4xnSDxMaL"),
        ("Antoni", "ErXwobaYiN019PkySvjV"),
        ("Elli", "MF3mGyEYCl7XYWbV9V6O"),
        ("Josh", "TxGEqnHWrfWFTfGW9XjX"),
        ("Adam", "pNInz6obpgDQGcFmaJgB"),
        ("Sam", "yoZ06aMxZJJ28mfd3POQ"),
        ("Domi", "AZnzlk1XvdvUeBnXmlld"),
    ],
    "ukr": [
        ("Sofiia", "96XEXOjZRHooATdYA8FY"),
        ("Vira", "nCqaTnIbLdME87OuQaZY"),
        ("Yaroslava", "0ZQZuw8Sn4cU0rN1Tm2K"),
        ("Solomiya", "yMBZR4SLoc24wOJLWAB2"),
        ("Bogdan", "jn6ifzU1eO5tfUZ2ZJVg"),
        ("Artem", "h9NSQvWZaC4NFusYsxT9"),
        ("Anton", "GVRiwBELe0czFUAJj0nX"),
        ("Yevhen", "TEyBWD5tAHAWqAGEv6yI"),
    ],
    "bul": [
        ("Peter K", "406EiNlYvqFqcz3vsnOm"),
        ("Milena", "M1ydWt7KnBCiuv4CnEDC"),
        ("Georgi", "31jwlwrRwpOA5yGuVAby"),
        ("Moonglow", "vnewfQdVVk9Y9DZWVRNm"),
        ("Kosta", "gdk0ZsvfAOobfbTtnx6p"),
        ("Silvi", "bUta4vyWcGUYrq5W9LDC"),
    ],
    "slk": [
        ("Alex", "5TUD5nYN251MvBggIfLu"),
        ("Jaro", "DXwrzy2wtKORwDTbsMwk"),
        ("Kvantova bublina", "YZozgTAUrNAtx3HAiy8V"),
        ("Julia", "9Nd358gE1qQp0pDh8FgP"),
        ("Peter", "d6IbhdqAKkXCCVuJjbie"),
        ("Luki Zajo", "Zai7B4Aol2bJtneyq0L1"),
        ("Andrej", "bYqmvVkXUBwLwYpGHGz3"),
        ("Jolana", "GsFe19Xn8iGqNR2RxINi"),
    ],
    "deu": [
        ("Anna", "Ah5UjbC5d1A2iCl9Lbe7"),
        ("Cornelia", "VGPs8uAVxETgmG3lNnZD"),
        ("Annika", "ViKqgJNeCiWZlYgHiAOO"),
        ("Emilia", "Dt2jDzhoZC0pZw5bmy2S"),
        ("Toby", "eEmoQJhC4SAEQpCINUov"),
        ("Otto", "FTNCalFNG5bRnkkaP5Ug"),
        ("Adrian", "aduJlSmEKqbhRQAAMzV2"),
        ("Stephan", "IWm8DnJ4NGjFI7QAM5lM"),
    ],
    "por": [
        ("Roberta", "RGymW84CSmfVugnA5tvA"),
        ("Keren", "33B4UnXyTNbgLmdEDh5P"),
        ("Ana Dias", "MZxV5lN3cv7hi1376O0m"),
        ("Raquel", "GDzHdQOi6jjf8zaXhCYD"),
        ("Rafael Valente", "dX7gRq1dIvLTgUaWpEFn"),
        ("Luka", "cFylwQo5ufGYUNyRS167"),
        ("Sandro Dutra", "qPfM2laM0pRL4rrZtBGl"),
        ("Borges", "9pDzHy2OpOgeXM8SeL0t"),
    ],
}


class ElevenLabsError(RuntimeError):
    pass


class NoNativeVoiceError(ElevenLabsError):
    """No native-speaker voices configured for the requested language."""


def has_native_voices(language: str) -> bool:
    """Return True if a native voice pool exists for ``language``."""
    ids = [v.strip() for v in settings.elevenlabs_voice_ids.split(",") if v.strip()]
    return bool(ids) or language in NATIVE_VOICES


def _voice_pool(language: str) -> list[tuple[str, str]]:
    ids = [v.strip() for v in settings.elevenlabs_voice_ids.split(",") if v.strip()]
    if ids:
        return [(vid, vid) for vid in ids]
    return NATIVE_VOICES.get(language, [])


def pick_voice(language: str) -> tuple[str, str]:
    """Return a random ``(name, voice_id)`` for this clip.

    Raises ``NoNativeVoiceError`` if no native voices are configured.
    """
    pool = _voice_pool(language)
    if not pool:
        raise NoNativeVoiceError(
            f"No native-speaker voices configured for '{language}'. "
            "Add entries to NATIVE_VOICES or set ELEVENLABS_VOICE_IDS."
        )
    return random.choice(pool)


def synthesize(text: str, language: str, dest_mp3: Path, *, voice_id: str) -> Path:
    """Synthesize ``text`` in ``language`` with ``voice_id`` into ``dest_mp3``."""
    if not settings.elevenlabs_api_key:
        raise ElevenLabsError("ELEVENLABS_API_KEY is not set.")

    payload: dict = {"text": text, "model_id": settings.elevenlabs_model}
    # flash/turbo models accept a language_code to pin pronunciation; v2 auto-detects.
    if "flash" in settings.elevenlabs_model or "turbo" in settings.elevenlabs_model:
        code = ISO_639_1.get(language)
        if code:
            payload["language_code"] = code

    url = ELEVENLABS_TTS.format(voice_id=voice_id)
    with httpx.Client(timeout=120) as client:
        resp = client.post(
            url,
            params={"output_format": "mp3_44100_128"},
            headers={
                "xi-api-key": settings.elevenlabs_api_key,
                "Content-Type": "application/json",
            },
            json=payload,
        )
        if resp.status_code != 200:
            raise ElevenLabsError(f"TTS failed ({resp.status_code}): {resp.text[:200]}")
        if len(resp.content) < 256:
            raise ElevenLabsError(f"TTS returned too little audio ({len(resp.content)} bytes)")
        dest_mp3.write_bytes(resp.content)
    return dest_mp3
