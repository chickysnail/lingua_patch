"""Speaking exercise: generate a practice sentence plus a theory handout,
transcribe the learner's voice, and evaluate the translation.

The sentence to translate is shown in Telegram. The HTML file is a pure
textbook chapter — vocabulary, real conjugation/declension tables and
construction notes written by a bilingual teacher — and never contains the
answer.
"""
from __future__ import annotations

import json
import logging
import random
import re
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
    "You are a bilingual language teacher who speaks both the learner's native language "
    "and the target language. You produce ONE speaking exercise plus the theory the learner "
    "needs to build the sentence themselves.\n"
    "Rules:\n"
    "- The source sentence is written in the NATIVE language; the learner will say it aloud "
    "in the target language. Keep it natural and appropriate to the level.\n"
    "- All explanations, notes and table headers are written in the NATIVE language; example "
    "forms are in the target language.\n"
    "- NEVER write the target-language translation of the source sentence, and never give a "
    "ready-made phrase that only has to be read out. Teach the pieces, not the answer.\n"
    "- 'grammar' must contain REAL tables: full conjugation of every verb the sentence needs, "
    "all persons of the tense the sentence actually requires (past sentence -> past tense table, "
    "not present), plus declensions, articles, pronoun or plural tables "
    "when the sentence needs them. Each table gets a title, a short explanation of when the "
    "form is used, header cells and rows. Rows must have exactly as many cells as headers.\n"
    "- In 'vocabulary', 'word' is the TARGET-language dictionary form and 'translation' is its "
    "native-language meaning — never the other way round.\n"
    "- 'notes' explains word order and how the pieces combine (2-4 short items).\n"
    "Respond ONLY with JSON:\n"
    '{"source_sentence": "...", '
    '"vocabulary": [{"word": "<target-language dictionary form>", '
    '"translation": "<native-language meaning>", '
    '"note": "grammatical info, e.g. verb, 2nd conjugation / noun, feminine"}], '
    '"grammar": [{"title": "...", "explanation": "...", "headers": ["...", "..."], '
    '"rows": [["...", "..."]]}], '
    '"notes": ["..."]}'
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

# HTML section headings per native language.
_LABELS: dict[str, dict[str, str]] = {
    "rus": {
        "title": "Теория",
        "vocabulary": "Слова",
        "grammar": "Грамматика",
        "notes": "Как собрать фразу",
        "filename": "теория",
    },
    "eng": {
        "title": "Theory",
        "vocabulary": "Vocabulary",
        "grammar": "Grammar",
        "notes": "Putting it together",
        "filename": "theory",
    },
    "ukr": {
        "title": "Теорія",
        "vocabulary": "Слова",
        "grammar": "Граматика",
        "notes": "Як скласти фразу",
        "filename": "теорія",
    },
}


def _client(client: OpenAI | None = None) -> OpenAI:
    if client is None:
        return OpenAI(api_key=settings.openai_api_key)
    return client


def _target_name(code: str) -> str:
    return ENGLISH_NAMES.get(code) or (LANGUAGES[code].name if code in LANGUAGES else code)


def _native_name(code: str) -> str:
    return NATIVE_NAMES.get(code, code)


def _labels(native_language: str) -> dict[str, str]:
    return _LABELS.get(native_language, _LABELS["eng"])


def _table(item: dict) -> dict:
    """Normalise one grammar table, dropping rows that do not fit the headers."""
    headers = [str(cell).strip() for cell in item.get("headers", []) if str(cell).strip()]
    rows: list[list[str]] = []
    for row in item.get("rows", []):
        if not isinstance(row, list):
            continue
        cells = [str(cell).strip() for cell in row]
        if not any(cells):
            continue
        if headers:
            cells = (cells + [""] * len(headers))[: len(headers)]
        rows.append(cells)
    return {
        "title": str(item.get("title", "")).strip(),
        "explanation": str(item.get("explanation", "")).strip(),
        "headers": headers,
        "rows": rows,
    }


def generate_exercise(
    language: str,
    native_language: str,
    difficulty: str | None = None,
    context: str | None = None,
    client: OpenAI | None = None,
) -> dict:
    """Generate a speaking exercise: source sentence + theory for the handout."""
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
        f"Generate the source sentence in {native_name} and the theory needed to translate it "
        f"into {target_name}: {target_name} vocabulary in dictionary form with its {native_name} "
        "meaning, full conjugation/declension tables for the forms involved, and short notes on "
        "word order. Do not reveal the translated sentence."
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
    grammar = [_table(item) for item in payload.get("grammar", []) if isinstance(item, dict)]
    return {
        "source_sentence": str(payload.get("source_sentence", "")).strip(),
        "vocabulary": [
            {
                "word": str(item.get("word", "")).strip(),
                "translation": str(item.get("translation", "")).strip(),
                "note": str(item.get("note", "")).strip(),
            }
            for item in payload.get("vocabulary", [])
            if str(item.get("word", "")).strip()
        ],
        "grammar": [table for table in grammar if table["rows"]],
        "notes": [str(note).strip() for note in payload.get("notes", []) if str(note).strip()],
        "language": language,
        "native_language": native_language,
    }


def handout_filename(language: str, native_language: str) -> str:
    """User-visible name for the theory file (Telegram shows it verbatim)."""
    name = LANGUAGES[language].name if language in LANGUAGES else _target_name(language)
    name = re.sub(r"\s*\([^)]*\)", "", name).strip() or _target_name(language)
    return f"{name} — {_labels(native_language)['filename']}.html"


def build_exercise_html(exercise: dict) -> Path:
    """Render the theory handout as an HTML file and return its path."""
    language = exercise["language"]
    native_language = exercise["native_language"]
    target_name = LANGUAGES[language].name if language in LANGUAGES else _target_name(language)
    labels = _labels(native_language)

    practice_dir = settings.media_dir / "practice"
    practice_dir.mkdir(parents=True, exist_ok=True)
    path = practice_dir / f"exercise_{uuid.uuid4().hex[:12]}.html"

    vocab_html = "".join(
        "<tr><td><strong>{word}</strong></td><td>{translation}</td><td>{note}</td></tr>".format(
            word=escape(item["word"]),
            translation=escape(item.get("translation", "")),
            note=escape(item.get("note", "")),
        )
        for item in exercise.get("vocabulary", [])
    )
    if vocab_html:
        vocab_html = (
            f"<h2>{escape(labels['vocabulary'])}</h2>\n"
            f"<table>{vocab_html}</table>"
        )

    sections: list[str] = []
    for raw_table in exercise.get("grammar", []):
        table = _table(raw_table)
        if not table["rows"]:
            continue
        head = (
            "<thead><tr>"
            + "".join(f"<th>{escape(cell)}</th>" for cell in table["headers"])
            + "</tr></thead>"
            if table["headers"]
            else ""
        )
        body = "".join(
            "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>"
            for row in table["rows"]
        )
        explanation = (
            f"<p>{escape(table['explanation'])}</p>" if table["explanation"] else ""
        )
        title = f"<h3>{escape(table['title'])}</h3>" if table["title"] else ""
        sections.append(f"{title}{explanation}<table>{head}<tbody>{body}</tbody></table>")
    grammar_html = (
        f"<h2>{escape(labels['grammar'])}</h2>\n" + "\n".join(sections) if sections else ""
    )

    notes = exercise.get("notes", [])
    notes_html = (
        f"<h2>{escape(labels['notes'])}</h2>\n<ul>"
        + "".join(f"<li>{escape(note)}</li>" for note in notes)
        + "</ul>"
        if notes
        else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="{escape(native_language)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(target_name)} — {escape(labels['title'])}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 40em; margin: 1em auto; padding: 0 1em; line-height: 1.5; background: #000; color: #fff; }}
h1 {{ font-size: 1.4em; }}
h2 {{ font-size: 1.15em; margin-top: 1.8em; }}
h3 {{ font-size: 1em; margin-top: 1.4em; }}
table {{ border-collapse: collapse; width: 100%; margin: 0.5em 0; }}
th, td {{ border: 1px solid #666; padding: 0.4em 0.5em; text-align: left; vertical-align: top; }}
th {{ background: #222; }}
</style>
</head>
<body>
<h1>{escape(target_name)} — {escape(labels['title'])}</h1>
{vocab_html}
{grammar_html}
{notes_html}
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
