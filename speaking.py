"""Speaking exercise: generate a practice sentence, build an HTML handout,
transcribe the learner's voice, and evaluate the translation.
"""
from __future__ import annotations

import json
import logging
import random
import uuid
from html import escape
from pathlib import Path

import httpx
from openai import OpenAI

from config import settings
from content import THEMES
from languages import ENGLISH_NAMES, ISO_639_1, LANGUAGES, NATIVE_NAMES

log = logging.getLogger(__name__)

_EXERCISE_SYSTEM = (
    "You create short speaking exercises for language learners. "
    "Given a target language, a native language, and a level, produce ONE simple source sentence "
    "in the native language that the learner should translate into the target language. "
    "Provide the exact target-language vocabulary and grammar forms needed to construct that sentence. "
    "Keep it appropriate to the level. "
    "Respond ONLY with JSON: "
    '{"source_sentence": "...", "vocabulary": [{"word": "...", "translation": "..."}], '
    '"grammar": [{"form": "...", "explanation": "..."}]}'
)

_EVAL_TEMPLATE = (
    "You evaluate a language learner's spoken translation. "
    "The learner was asked to translate the following source sentence from {native_name} to {target_name}:\n"
    '"{source_sentence}"\n\n'
    "Their transcribed spoken answer is:\n"
    '"{transcription}"\n\n'
    "Is the answer a correct translation? Be somewhat lenient about minor grammar or word-choice issues. "
    "Do not be strict about punctuation. "
    'Respond ONLY with JSON: '
    '{{"status": "Correct" | "Incorrect" | "Almost correct", '
    '"feedback": "short suggestions in {native_name}"}}'
)


def _client(client: OpenAI | None = None) -> OpenAI:
    if client is None:
        return OpenAI(api_key=settings.openai_api_key)
    return client


def _target_name(code: str) -> str:
    return ENGLISH_NAMES.get(code) or (LANGUAGES[code].name if code in LANGUAGES else code)


def _native_name(code: str) -> str:
    return NATIVE_NAMES.get(code, code)


def generate_exercise(
    language: str,
    native_language: str,
    difficulty: str | None = None,
    context: str | None = None,
    client: OpenAI | None = None,
) -> dict:
    """Generate a speaking exercise (source sentence + vocab + grammar)."""
    client = _client(client)
    target_name = _target_name(language)
    native_name = _native_name(native_language)

    parts = [
        f"Target language: {target_name} ({language})",
        f"Native language: {native_name} ({native_language})",
        f"Level: {difficulty or 'mixed'}",
    ]
    if context:
        parts.append(
            "Context (a recent patch the user saw — use it as inspiration, "
            "but do not copy it and keep the exercise short):\n" + context
        )
    else:
        parts.append(f"Theme: {random.choice(THEMES)}")
    parts.append(
        "Generate a source sentence and the vocabulary + grammar the learner needs to translate it. "
        "Vocabulary should be dictionary forms. Grammar should explain each form briefly."
    )

    resp = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": _EXERCISE_SYSTEM},
            {"role": "user", "content": "\n".join(parts)},
        ],
        response_format={"type": "json_object"},
        temperature=0.8,
    )
    payload = json.loads(resp.choices[0].message.content or "{}")
    return {
        "source_sentence": str(payload.get("source_sentence", "")).strip(),
        "vocabulary": [
            {
                "word": str(item.get("word", "")).strip(),
                "translation": str(item.get("translation", "")).strip(),
            }
            for item in payload.get("vocabulary", [])
            if str(item.get("word", "")).strip()
        ],
        "grammar": [
            {
                "form": str(item.get("form", "")).strip(),
                "explanation": str(item.get("explanation", "")).strip(),
            }
            for item in payload.get("grammar", [])
            if str(item.get("form", "")).strip()
        ],
        "language": language,
        "native_language": native_language,
    }


def build_exercise_html(exercise: dict) -> Path:
    """Render the exercise as a simple HTML file and return its path."""
    language = exercise["language"]
    native_language = exercise["native_language"]
    target_name = _target_name(language)
    native_name = _native_name(native_language)

    practice_dir = settings.media_dir / "practice"
    practice_dir.mkdir(parents=True, exist_ok=True)
    path = practice_dir / f"exercise_{uuid.uuid4().hex[:12]}.html"

    vocab_items = exercise.get("vocabulary", [])
    grammar_items = exercise.get("grammar", [])

    vocab_html = "".join(
        f'<li><strong>{escape(item["word"])}</strong> — {escape(item["translation"])}</li>'
        for item in vocab_items
    )
    grammar_html = "".join(
        f"<tr><td>{escape(item['form'])}</td><td>{escape(item['explanation'])}</td></tr>"
        for item in grammar_items
    )

    html = f"""<!DOCTYPE html>
<html lang="{escape(native_language)}">
<head>
<meta charset="utf-8">
<title>Practice: {escape(target_name)}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 40em; margin: 1em auto; padding: 0 1em; line-height: 1.5; background: #000; color: #fff; }}
h1 {{ font-size: 1.4em; }}
h2 {{ font-size: 1.1em; margin-top: 1.5em; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #666; padding: 0.5em; text-align: left; vertical-align: top; }}
th {{ background: #222; }}
</style>
</head>
<body>
<h1>Practice: translate this sentence</h1>
<p><strong>Source ({escape(native_name)}):</strong> {escape(exercise.get('source_sentence', ''))}</p>

<h2>Vocabulary</h2>
<ul>
{vocab_html}
</ul>

<h2>Grammar</h2>
<table>
<tr><th>Form</th><th>Explanation</th></tr>
{grammar_html}
</table>

<p><em>Return to Telegram and reply with a voice message in {escape(target_name)}.</em></p>
</body>
</html>"""
    path.write_text(html, encoding="utf-8")
    return path


def transcribe_voice(audio_path: Path, language: str) -> str | None:
    """Transcribe a voice file using ElevenLabs Scribe.

    Returns the transcribed text, or None when the model returns empty text.
    Raises on API errors so the caller can decide what to tell the user.
    """
    if not settings.elevenlabs_stt_api_key:
        raise RuntimeError("ELEVENLABS_STT_API_KEY is not set.")

    code = ISO_639_1.get(language)
    data: dict[str, str] = {"model_id": "scribe_v1"}
    if code:
        data["language_code"] = code

    url = "https://api.elevenlabs.io/v1/speech-to-text"
    with httpx.Client(timeout=120) as client, audio_path.open("rb") as f:
        resp = client.post(
            url,
            headers={"xi-api-key": settings.elevenlabs_stt_api_key},
            data=data,
            files={"file": (audio_path.name, f, "audio/ogg")},
        )
    if resp.status_code != 200:
        log.warning("STT failed (%s): %s", resp.status_code, resp.text[:200])
        raise RuntimeError(f"STT failed ({resp.status_code})")

    payload = resp.json()
    text = str(payload.get("text", "")).strip()
    return text if text else None


def evaluate_translation(
    source_sentence: str,
    transcription: str,
    language: str,
    native_language: str,
    client: OpenAI | None = None,
) -> dict:
    """Evaluate the learner's spoken translation."""
    client = _client(client)
    system = _EVAL_TEMPLATE.format(
        native_name=_native_name(native_language),
        target_name=_target_name(language),
        source_sentence=source_sentence,
        transcription=transcription,
    )
    resp = client.chat.completions.create(
        model=settings.openai_model,
        messages=[{"role": "system", "content": system}],
        response_format={"type": "json_object"},
        temperature=0.3,
    )
    payload = json.loads(resp.choices[0].message.content or "{}")
    status = str(payload.get("status", "Incorrect")).strip()
    feedback = str(payload.get("feedback", "")).strip()

    # Keep the first line short and clean.
    words = status.split()
    if len(words) > 2:
        lowered = status.lower()
        if "almost" in lowered and "correct" in lowered:
            status = "Almost correct"
        elif "correct" in lowered:
            status = "Correct"
        else:
            status = "Incorrect"
    else:
        # Capitalise single-word statuses.
        status = " ".join(w.capitalize() for w in words)

    return {"status": status, "feedback": feedback}
