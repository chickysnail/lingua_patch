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

MAX_VOCABULARY_ITEMS = 8
MAX_GRAMMAR_TABLES = 4
MAX_ROWS_PER_TABLE = 12
MAX_KEY_PHRASES = 5
MAX_NOTES = 4
MAX_CELL_CHARS = 200
MAX_HTML_BYTES = 100_000


class STTError(RuntimeError):
    """Speech-to-text service failed after the request was attempted."""


class STTConfigurationError(STTError):
    """Speech-to-text credentials are missing or rejected."""


_EXERCISE_SYSTEM = (
    "You are a bilingual language teacher who speaks both the learner's native language "
    "and the target language. You produce ONE speaking exercise plus the theory the learner "
    "needs to build the sentence themselves.\n"
    "Rules:\n"
    "- The source sentence is written in the NATIVE language; the learner will say it aloud "
    "in the target language. Keep it natural and appropriate to the level.\n"
    "- All explanations, notes and table headers are written in the NATIVE language; example "
    "forms are in the target language. Use a warm, conversational Duolingo Guidebook tone, "
    "with playful short tip titles.\n"
    "- Every table header, including the subject and verb headers, must be written in the "
    "learner's NATIVE language, never English placeholders.\n"
    "- NEVER write the target-language translation of the source sentence, and never give a "
    "ready-made phrase that only has to be read out. Teach the pieces, not the answer.\n"
    "- 'grammar' must contain REAL tables: full conjugation of every verb the sentence needs, "
    "all persons of the tense the sentence actually requires (past sentence -> past tense table, "
    "not present), plus declensions, articles, pronoun or plural tables "
    "when the sentence needs them. Each table gets a title, a short explanation of when the "
    "form is used, header cells and rows. For a conjugation table, use exactly TWO columns: "
    "the native-language equivalent of 'subject' and 'verb/form'; every row is exactly "
    "[target pronoun + native gloss, target-language form]. Never transpose the table. "
    "Rows must have exactly as many cells as headers. "
    "Table row labels MUST be target-language pronoun plus native gloss exactly like "
    "'eu (я)', 'tu (ты)', 'ele/ela (он/она)', 'nós (мы)', 'vocês (вы)', "
    "'eles/elas (они)' where applicable. Never use the academic words "
    "'единственное', 'множественное', or 'лицо' in table labels.\n"
    "- Every conjugated or declined form must be the real, correct form for that exact "
    "pronoun and tense. Do not repeat a form across different pronouns unless the language "
    "genuinely shares that form. Re-check every table row against the actual paradigm before "
    "returning the JSON.\n"
    "- Each grammar item also has a short native-language 'notice' nudge (or empty string) "
    "and one concrete 'example' object with target and native strings.\n"
    "- Include 3-5 useful 'key_phrases' in the target language with native translations. "
    "They must be different phrases, not the source sentence or its translation.\n"
    "- In 'vocabulary', 'word' is the TARGET-language dictionary form and 'translation' is its "
    "native-language meaning — never the other way round.\n"
    "- 'notes' explains word order and how the pieces combine (2-4 short items).\n"
    "- Keep the handout concise: no more than 8 vocabulary items, 4 grammar tables, "
    "12 rows per table, 5 key phrases, and 4 notes; keep each cell near 200 characters.\n"
    "Respond ONLY with JSON:\n"
    '{"source_sentence": "...", '
    '"vocabulary": [{"word": "<target-language dictionary form>", '
    '"translation": "<native-language meaning>", '
    '"note": "grammatical info, e.g. verb, 2nd conjugation / noun, feminine"}], '
    '"grammar": [{"title": "...", "explanation": "...", '
    '"headers": ["...", "..."], '
    '"rows": [["<target pronoun> (<native gloss>)", "<target form>"]], "notice": "...", '
    '"example": {"target": "...", "native": "..."}}], '
    '"key_phrases": [{"target": "...", "native": "..."}], '
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
        "task": "Задание",
        "vocabulary": "Слова",
        "grammar": "Грамматика",
        "key_phrases": "Полезные фразы",
        "notice": "Обрати внимание",
        "notes": "Как собрать фразу",
        "filename": "теория",
    },
    "eng": {
        "title": "Theory",
        "task": "Task",
        "vocabulary": "Vocabulary",
        "grammar": "Grammar",
        "key_phrases": "Key phrases",
        "notice": "Notice",
        "notes": "Putting it together",
        "filename": "theory",
    },
    "ukr": {
        "title": "Теорія",
        "task": "Завдання",
        "vocabulary": "Слова",
        "grammar": "Граматика",
        "key_phrases": "Корисні фрази",
        "notice": "Зверни увагу",
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


def _clip(value: object) -> str:
    text = str(value).strip()
    if len(text) <= MAX_CELL_CHARS:
        return text
    return text[: MAX_CELL_CHARS - 1].rstrip() + "…"


def _table(item: object) -> dict:
    """Normalise one grammar table, dropping rows that do not fit the headers."""
    if not isinstance(item, dict):
        return {
            "title": "",
            "explanation": "",
            "headers": [],
            "rows": [],
            "notice": "",
            "example": {"target": "", "native": ""},
        }
    title = _clip(item.get("title", ""))
    raw_headers = item.get("headers", [])
    headers = (
        [_clip(cell) for cell in raw_headers if str(cell).strip()]
        if isinstance(raw_headers, list)
        else []
    )
    if any(header == "..." or "<" in header or ">" in header for header in headers):
        headers = []
    rows: list[list[str]] = []
    raw_rows = item.get("rows", [])
    for row in (raw_rows if isinstance(raw_rows, list) else [])[:MAX_ROWS_PER_TABLE]:
        if not isinstance(row, list):
            continue
        cells = [_clip(cell) for cell in row]
        if not any(cells):
            continue
        if headers:
            cells = (cells + [""] * len(headers))[: len(headers)]
        rows.append(cells)
    return {
        "title": title,
        "explanation": _clip(item.get("explanation", "")),
        "headers": headers,
        "rows": rows,
        "notice": _clip(item.get("notice", "")),
        "example": _example(item.get("example")),
    }


def _example(item: object) -> dict[str, str]:
    if not isinstance(item, dict):
        return {"target": "", "native": ""}
    return {
        "target": _clip(item.get("target", "")),
        "native": _clip(item.get("native", "")),
    }


def _key_phrase(item: object) -> dict[str, str] | None:
    if not isinstance(item, dict):
        return None
    target = _clip(item.get("target", ""))
    native = _clip(item.get("native", ""))
    if not target or not native:
        return None
    return {"target": target, "native": native}


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
        model=settings.openai_exercise_model,
        messages=[
            {"role": "system", "content": _EXERCISE_SYSTEM},
            {"role": "user", "content": "\n".join(parts)},
        ],
        response_format={"type": "json_object"},
        temperature=0.8,
    )
    payload = json.loads(resp.choices[0].message.content or "{}")
    grammar = [
        _table(item)
        for item in payload.get("grammar", [])
        if isinstance(item, dict)
    ]
    key_phrases = [
        phrase
        for item in payload.get("key_phrases", [])
        if (phrase := _key_phrase(item)) is not None
    ]
    return {
        "source_sentence": _clip(payload.get("source_sentence", "")),
        "vocabulary": [
            {
                "word": _clip(item.get("word", "")),
                "translation": _clip(item.get("translation", "")),
                "note": _clip(item.get("note", "")),
            }
            for item in payload.get("vocabulary", [])[:MAX_VOCABULARY_ITEMS]
            if isinstance(item, dict) and str(item.get("word", "")).strip()
        ],
        "grammar": [table for table in grammar[:MAX_GRAMMAR_TABLES] if table["rows"]],
        "key_phrases": key_phrases[:MAX_KEY_PHRASES],
        "notes": [_clip(note) for note in payload.get("notes", [])[:MAX_NOTES] if str(note).strip()],
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
    native_name = _native_name(native_language)
    labels = _labels(native_language)
    source_sentence = str(exercise.get("source_sentence", "")).strip()

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
        notice = (
            f"<p class=\"notice\"><strong>{escape(labels['notice'])}:</strong> "
            f"{escape(table['notice'])}</p>"
            if table["notice"]
            else ""
        )
        example = table["example"]
        example_html = (
            "<p class=\"example\"><strong>"
            f"{escape(example['target'])}</strong> — {escape(example['native'])}</p>"
            if example["target"] and example["native"]
            else ""
        )
        sections.append(
            f"{title}{explanation}<table>{head}<tbody>{body}</tbody></table>"
            f"{notice}{example_html}"
        )
    grammar_html = (
        f"<h2>{escape(labels['grammar'])}</h2>\n" + "\n".join(sections) if sections else ""
    )

    phrase_rows = "".join(
        f"<tr><td><strong>{escape(phrase['target'])}</strong></td>"
        f"<td>{escape(phrase['native'])}</td></tr>"
        for item in exercise.get("key_phrases", [])
        if (phrase := _key_phrase(item)) is not None
    )
    phrases_html = (
        f"<h2>{escape(labels['key_phrases'])}</h2><table>"
        f"<thead><tr><th>{escape(target_name)}</th>"
        f"<th>{escape(native_name)}</th></tr></thead>"
        f"<tbody>{phrase_rows}</tbody></table>"
        if phrase_rows
        else ""
    )

    notes = exercise.get("notes", [])
    notes_html = (
        f"<h2>{escape(labels['notes'])}</h2>\n<ul>"
        + "".join(f"<li>{escape(note)}</li>" for note in notes)
        + "</ul>"
        if notes
        else ""
    )

    def _render(phrases: str, notes_section: str) -> str:
        return f"""<!DOCTYPE html>
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
.task {{ border: 1px solid #666; border-radius: 0.5em; padding: 0.2em 0.8em; }}
.notice {{ background: #332b00; border-left: 0.3em solid #f0c420; padding: 0.5em 0.8em; }}
.example {{ background: #181818; padding: 0.5em 0.8em; }}
</style>
</head>
<body>
<section class="task"><h2>{escape(labels['task'])}</h2><p><strong>{escape(source_sentence)}</strong></p></section>
<h1>{escape(target_name)} — {escape(labels['title'])}</h1>
{vocab_html}
{grammar_html}
{phrases}
{notes_section}
</body>
</html>"""
    html = _render(phrases_html, notes_html)
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        log.warning("Practice handout exceeded %d bytes; dropping key phrases.", MAX_HTML_BYTES)
        html = _render("", notes_html)
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        log.warning("Practice handout still exceeded %d bytes; dropping notes.", MAX_HTML_BYTES)
        html = _render("", "")
    path.write_text(html, encoding="utf-8")
    return path


def transcribe_voice(audio_path: Path, language: str) -> str | None:
    """Transcribe a voice file using ElevenLabs Scribe.

    Returns the transcribed text, or None when the model returns empty text.
    Raises on API errors so the caller can decide what to tell the user.
    """
    api_key = settings.elevenlabs_stt_api_key or settings.elevenlabs_api_key
    key_source = "dedicated STT key" if settings.elevenlabs_stt_api_key else "main ElevenLabs key"
    if not api_key:
        raise STTConfigurationError(
            "Neither ELEVENLABS_STT_API_KEY nor ELEVENLABS_API_KEY is set."
        )

    code = ISO_639_1.get(language)
    data: dict[str, str] = {"model_id": "scribe_v1"}
    if code:
        data["language_code"] = code

    url = "https://api.elevenlabs.io/v1/speech-to-text"
    with httpx.Client(timeout=120) as client, audio_path.open("rb") as f:
        resp = client.post(
            url,
            headers={"xi-api-key": api_key},
            data=data,
            files={"file": (audio_path.name, f, "audio/ogg")},
        )
    if resp.status_code != 200:
        snippet = resp.text[:200].replace("\n", " ")
        log.warning("STT failed using %s (status=%s): %s", key_source, resp.status_code, snippet)
        if "missing_permissions" in snippet.lower() or "speech_to_text" in snippet.lower():
            error = (
                f"STT failed using {key_source} (status={resp.status_code}): "
                "the ElevenLabs key lacks the speech_to_text permission. "
                f"{snippet}"
            )
        else:
            error = f"STT failed using {key_source} (status={resp.status_code}): {snippet}"
        if resp.status_code in (401, 403):
            raise STTConfigurationError(error)
        raise STTError(error)

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
        model=settings.openai_exercise_model,
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
