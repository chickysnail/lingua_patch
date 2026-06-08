"""Content pipeline: fetch native-audio sentences from Tatoeba, build the
vocabulary breakdown with OpenAI, and produce a Telegram-ready voice note.

Nothing here is YouTube/movie scraping: Tatoeba sentences are crowd-sourced and
openly licensed (CC-BY), so re-sending the audio in Telegram is allowed as long
as we keep the attribution that each row carries.
"""
from __future__ import annotations

import json
import logging
import random
import subprocess
from pathlib import Path
from urllib.parse import quote

import httpx

from config import settings
from languages import ENGLISH_NAMES, LANGUAGES, NATIVE_NAMES

log = logging.getLogger(__name__)

TATOEBA_SEARCH = "https://api.tatoeba.org/unstable/sentences"
# The unstable API advertises a /unstable/audio/<id>/file download URL that
# currently 404s; the classic endpoint below is the one that actually serves
# the mp3, so we build the URL ourselves from the audio id.
TATOEBA_AUDIO = "https://tatoeba.org/audio/download/{audio_id}"


class TatoebaError(RuntimeError):
    pass


def youglish_url(word: str, language: str) -> str:
    """Build a YouGlish 'explore this word' link (no API key required)."""
    slug = LANGUAGES[language].youglish if language in LANGUAGES else language
    return f"https://youglish.com/pronounce/{quote(word)}/{slug}"


def fetch_sentences(language: str, native_language: str, limit: int) -> list[dict]:
    """Return up to ``limit`` random sentences in ``language`` that have audio
    and a translation into ``native_language``.
    """
    params = {
        "lang": language,
        "has_audio": "yes",
        "include": "audios",
        "trans:lang": native_language,
        "showtrans": "all",
        "sort": "random",
        "limit": str(limit),
    }
    with httpx.Client(timeout=30) as client:
        resp = client.get(TATOEBA_SEARCH, params=params)
        resp.raise_for_status()
        data = resp.json()
    if "data" not in data:
        raise TatoebaError(f"Unexpected Tatoeba response: {data}")
    return data["data"]


def extract_native_translation(sentence: dict, native_language: str) -> str | None:
    for tr in sentence.get("translations", []):
        if tr.get("lang") == native_language and tr.get("text"):
            return tr["text"]
    return None


def download_audio(audio_id: int, dest: Path) -> Path:
    url = TATOEBA_AUDIO.format(audio_id=audio_id)
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        if not resp.content or len(resp.content) < 256:
            raise TatoebaError(f"Audio {audio_id} download too small ({len(resp.content)} bytes)")
        dest.write_bytes(resp.content)
    return dest


def to_voice_ogg(mp3_path: Path, ogg_path: Path) -> Path:
    """Convert an mp3 to OGG/Opus so Telegram renders it as a true voice note."""
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(mp3_path), "-c:a", "libopus", "-b:a", "32k", str(ogg_path)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return ogg_path


_VOCAB_SYSTEM = (
    "You are a concise language-learning assistant. Given a sentence in the target "
    "language and its translation in the learner's native language, identify the 3-5 "
    "words in the target sentence that are MOST DIFFERENT from the learner's native "
    "language (i.e. false friends or words a native speaker would not guess). For each, "
    "give the dictionary form, the native-language translation, and a 2-4 word usage hint. "
    "Respond ONLY with JSON: "
    '{"vocabulary": [{"word": "...", "translation": "...", "context": "..."}]}'
)


def build_vocabulary(
    transcript: str,
    translation: str | None,
    language: str,
    native_language: str,
    client=None,
) -> list[dict[str, str]]:
    """Use OpenAI to pick the words that differ most from the native language.

    Returns an empty list (rather than raising) on any failure so seeding a
    single noisy sentence never aborts the whole run.
    """
    if client is None:
        from openai import OpenAI

        client = OpenAI(api_key=settings.openai_api_key)

    user_msg = (
        f"Target language: {language}\nNative language: {native_language}\n"
        f"Sentence: {transcript}\nTranslation: {translation or '(none provided)'}"
    )
    try:
        resp = client.chat.completions.create(
            model=settings.openai_model,
            messages=[
                {"role": "system", "content": _VOCAB_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        payload = json.loads(resp.choices[0].message.content or "{}")
        vocab = payload.get("vocabulary", [])
        clean: list[dict[str, str]] = []
        for item in vocab:
            word = str(item.get("word", "")).strip()
            if not word:
                continue
            clean.append(
                {
                    "word": word,
                    "translation": str(item.get("translation", "")).strip(),
                    "context": str(item.get("context", "")).strip(),
                }
            )
        return clean[:5]
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, log for debugging
        log.warning("Vocabulary generation failed for %r: %s", transcript, exc)
        return []


# --------------------------------------------------------------------------- #
# ElevenLabs / TTS content: generate the text to be spoken
# --------------------------------------------------------------------------- #
THEMES = [
    "morning coffee", "the weather today", "commuting to work", "cooking dinner",
    "weekend plans", "small talk with a neighbour", "grocery shopping",
    "feeling tired after a long day", "a funny everyday moment", "calling a friend",
    "the changing seasons", "a small frustration", "trying something new",
    "running late", "a quiet evening at home", "planning a trip",
]


def _snippet_system(length: str) -> str:
    if length == "short":
        span = "ONE short, natural sentence (about 4-8 seconds when read aloud)"
        words = "2-3"
    else:
        span = "2-4 short, natural sentences (about 12-20 seconds when read aloud)"
        words = "3-5"
    return (
        "You write tiny, natural audio snippets for passive language learning. Given a "
        f"target language, the learner's native language, and a theme, produce {span} in "
        "the TARGET language on that theme — conversational, like something a native "
        "speaker would actually say, not a textbook example. Then provide the full "
        f"translation in the native language, and pick the {words} words MOST DIFFERENT "
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
    client=None,
    length: str = "long",
) -> dict:
    """Generate a themed snippet (transcript + translation + vocabulary).

    ``length`` is "short" (one sentence) or "long" (2-4 sentences). One OpenAI call
    produces everything the TTS path needs. Raises on failure so the seeder can skip.
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
            {"role": "system", "content": _snippet_system(length)},
            {"role": "user", "content": user_msg},
        ],
        response_format={"type": "json_object"},
        temperature=0.8,
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
