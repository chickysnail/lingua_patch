"""ElevenLabs text-to-speech: turn generated text into a voice-note mp3.

Uses the REST API directly (httpx) so there is no extra SDK dependency. The API
key only needs the ``text_to_speech`` permission — voices come from a curated
pool of stable premade multilingual voices, so ``voices_read`` is not required.
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

# Stable premade voices available to every ElevenLabs account; all work with the
# multilingual model. (name, voice_id) — the name is shown as soft attribution.
CURATED_VOICES: list[tuple[str, str]] = [
    ("Rachel", "21m00Tcm4TlvDq8ikWAM"),
    ("Bella", "EXAVITQu4vr4xnSDxMaL"),
    ("Antoni", "ErXwobaYiN019PkySvjV"),
    ("Elli", "MF3mGyEYCl7XYWbV9V6O"),
    ("Josh", "TxGEqnHWrfWFTfGW9XjX"),
    ("Adam", "pNInz6obpgDQGcFmaJgB"),
    ("Sam", "yoZ06aMxZJJ28mfd3POQ"),
    ("Domi", "AZnzlk1XvdvUeBnXmlld"),
]

# Native-speaker voices from the ElevenLabs Voice Library, keyed by ISO 639-3.
# Preferred over CURATED_VOICES when generating content for a matching language.
NATIVE_VOICES: dict[str, list[tuple[str, str]]] = {
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
}


class ElevenLabsError(RuntimeError):
    pass


def _voice_pool(language: str = "") -> list[tuple[str, str]]:
    ids = [v.strip() for v in settings.elevenlabs_voice_ids.split(",") if v.strip()]
    if ids:
        return [(vid, vid) for vid in ids]
    if language and language in NATIVE_VOICES:
        return NATIVE_VOICES[language]
    return CURATED_VOICES


def pick_voice(language: str = "") -> tuple[str, str]:
    """Return a random ``(name, voice_id)`` for this clip."""
    return random.choice(_voice_pool(language))


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
