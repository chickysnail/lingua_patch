"""Content pipeline: generate language-learning patches entirely with AI.

OpenAI produces the sentence text (randomised length), translation, and
vocabulary breakdown; ElevenLabs voices the sentence. No external corpus
dependency — the pool grows on demand as users consume patches.
"""
from __future__ import annotations

import json
import logging
import random
import subprocess
from pathlib import Path
from urllib.parse import quote

from config import settings
from languages import ENGLISH_NAMES, LANGUAGES, NATIVE_NAMES

log = logging.getLogger(__name__)


def youglish_url(word: str, language: str) -> str:
    """Build a YouGlish 'explore this word' link (no API key required)."""
    slug = LANGUAGES[language].youglish if language in LANGUAGES else language
    return f"https://youglish.com/pronounce/{quote(word)}/{slug}"


def to_voice_ogg(mp3_path: Path, ogg_path: Path) -> Path:
    """Convert an mp3 to OGG/Opus so Telegram renders it as a true voice note."""
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(mp3_path), "-c:a", "libopus", "-b:a", "32k", str(ogg_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return ogg_path


# --------------------------------------------------------------------------- #
# AI snippet generation (OpenAI text + vocab in one call)
# --------------------------------------------------------------------------- #
THEMES = [
    "morning coffee", "the weather today", "commuting to work", "cooking dinner",
    "weekend plans", "small talk with a neighbour", "grocery shopping",
    "feeling tired after a long day", "a funny everyday moment", "calling a friend",
    "the changing seasons", "a small frustration", "trying something new",
    "running late", "a quiet evening at home", "planning a trip",
    "ordering at a café", "asking for directions", "meeting someone new",
    "a childhood memory", "weekend morning routine", "talking about a movie",
    "waiting in line", "a rainy day", "celebrating a small win",
    "complaining about technology", "a walk in the park", "late-night snack",
    "packing for a trip", "a misunderstanding",
]

_SNIPPET_SYSTEM = (
    "You write tiny, natural audio snippets for passive language learning. Given a "
    "target language, the learner's native language, and a theme, produce a snippet of "
    "RANDOM length: sometimes just one short sentence (4-8 seconds when read aloud), "
    "sometimes 2-4 sentences (12-20 seconds). Vary it each time — the learner should "
    "never know what length to expect. The text must be conversational, like something "
    "a native speaker would actually say, not a textbook example. Then provide the full "
    "translation in the native language, and pick the 3-5 words MOST DIFFERENT "
    "from the native language (false friends or words a native speaker would not "
    "guess), each with its dictionary form, the native-language translation, and a "
    "2-4 word usage hint. Respond ONLY with JSON: "
    '{"transcript": "...", "translation": "...", '
    '"vocabulary": [{"word": "...", "translation": "...", "context": "..."}]}'
)


def generate_snippet(
    language: str,
    native_language: str,
    theme: str | None = None,
    client: object | None = None,
) -> dict:
    """Generate a themed snippet (transcript + translation + vocabulary).

    One OpenAI call produces everything the TTS path needs.
    Raises on failure so the caller can skip and retry.
    """
    if client is None:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)

    theme = theme or random.choice(THEMES)
    lang_name = ENGLISH_NAMES.get(language) or (LANGUAGES[language].name if language in LANGUAGES else language)
    native_name = NATIVE_NAMES.get(native_language, native_language)
    user_msg = (
        f"Target language: {lang_name} ({language})\n"
        f"Native language: {native_name} ({native_language})\n"
        f"Theme: {theme}"
    )
    resp = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": _SNIPPET_SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        response_format={"type": "json_object"},
        temperature=0.9,
    )
    payload = json.loads(resp.choices[0].message.content or "{}")
    transcript = str(payload.get("transcript", "")).strip()
    if not transcript:
        raise ValueError("OpenAI returned an empty transcript")
    translation = str(payload.get("translation", "")).strip() or None

    vocab: list[dict[str, str]] = []
    for item in payload.get("vocabulary", []):
        word = str(item.get("word", "")).strip()
        if not word:
            continue
        vocab.append(
            {
                "word": word,
                "translation": str(item.get("translation", "")).strip(),
                "context": str(item.get("context", "")).strip(),
            }
        )
    return {
        "transcript": transcript,
        "translation": translation,
        "vocabulary": vocab[:5],
        "theme": theme,
    }
