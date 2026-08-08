"""Speaking exercise: generate a practice sentence plus theory, transcribe the
learner's voice, and talk them through the exercise.

Once the task is sent, every learner turn — spoken (the normal way) or typed —
is answered in the context of the whole session by a bilingual speaker who
never leaves the topic of language learning.

The sentence is generated on its own so the learner gets the task immediately;
the theory is generated afterwards and folded into the same rich message. The
vocabulary table is visible right away and every other hint — each grammar
table, the key phrases, the word-order notes — sits in its own collapsible
block. It is a pure textbook chapter and never contains the answer.
"""
from __future__ import annotations

import json
import logging
import random
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
# Telegram caps a rich message at 32768 characters; stay well below it.
MAX_RICH_CHARS = 30_000


class STTError(RuntimeError):
    """Speech-to-text service failed after the request was attempted."""


class STTConfigurationError(STTError):
    """Speech-to-text credentials are missing or rejected."""


_SENTENCE_SYSTEM = (
    "You are a bilingual language teacher. You invent ONE sentence for a speaking exercise: "
    "it is written in the learner's NATIVE language and the learner will say it aloud in the "
    "target language. Keep it natural, everyday and appropriate to the level, one sentence "
    "only. NEVER write the target-language translation.\n"
    'Respond ONLY with JSON: {"source_sentence": "..."}'
)

_THEORY_SYSTEM = (
    "You are a bilingual language teacher who speaks both the learner's native language "
    "and the target language. Given a source sentence in the learner's native language, you "
    "produce the theory the learner needs to say it in the target language themselves.\n"
    "Rules:\n"
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
    "- Keep the theory concise: no more than 8 vocabulary items, 4 grammar tables, "
    "12 rows per table, 5 key phrases, and 4 notes; keep each cell near 200 characters.\n"
    "Respond ONLY with JSON:\n"
    '{"vocabulary": [{"word": "<target-language dictionary form>", '
    '"translation": "<native-language meaning>", '
    '"note": "grammatical info, e.g. verb, 2nd conjugation / noun, feminine"}], '
    '"grammar": [{"title": "...", "explanation": "...", '
    '"headers": ["...", "..."], '
    '"rows": [["<target pronoun> (<native gloss>)", "<target form>"]], "notice": "...", '
    '"example": {"target": "...", "native": "..."}}], '
    '"key_phrases": [{"target": "...", "native": "..."}], '
    '"notes": ["..."]}'
)

MAX_HISTORY_TURNS = 20
VERDICTS = ("correct", "almost", "incorrect", "none")

_CHAT_TEMPLATE = (
    "You are a person who grew up speaking both {native_name} and {target_name} and you are "
    "sitting next to a learner during ONE speaking exercise. You were the one who set the "
    "task: say this {native_name} sentence out loud in {target_name}:\n"
    '"{source_sentence}"\n\n'
    "You see the whole conversation of this exercise. Every learner turn is labelled: "
    "[voice] is a speech-to-text transcript of what they said out loud — expect small "
    "recognition artefacts (missing punctuation, a homophone, a swallowed ending) and never "
    "build a correction on something that is obviously just misheard; [text] is typed, may be "
    "in any language and is often a question rather than an attempt.\n"
    "Decide what the latest turn is and answer accordingly:\n"
    "- An attempt at the exercise: reply the way a bilingual friend actually would. If it was "
    "right, confirm it warmly and, if useful, add one natural-sounding alternative. If it was "
    "not, say the whole natural {target_name} sentence they were reaching for, then explain in "
    "at most two short sentences WHY, in terms of meaning or a rule "
    "(\"'gostar' always carries 'de' before what you like\"). "
    "NEVER produce a list of mechanical edit operations such as \"add X after Y, use Z "
    "instead of W, remove V\" — that reads like a puzzle instead of speech. Do not enumerate "
    "every deviation: give the correct sentence and the one or two things worth remembering.\n"
    "- A question about the exercise, the language, a word, grammar or pronunciation: answer "
    "it plainly and briefly, then invite them to try the sentence again if they have not "
    "said it correctly yet.\n"
    "- Anything that is not language learning (small talk, personal advice, news, code, "
    "requests to be a general assistant): decline in one friendly sentence and bring them "
    "back to the exercise. You only ever talk about learning {target_name}.\n"
    "Style: write in {native_name}; only examples and corrected sentences are in "
    "{target_name}. Address the learner informally. Keep it under 60 words, plain sentences, "
    "no bullet lists, no headings, no emoji spam. When you invite another attempt, invite "
    "them to SAY it out loud — never suggest writing or typing it.\n"
    'Respond ONLY with JSON: {{"verdict": "correct" | "almost" | "incorrect" | "none", '
    '"reply": "your answer in {native_name}"}} '
    'where "verdict" describes the latest attempt and is "none" when the latest turn is not '
    "an attempt."
)

# Section headings per native language.
_LABELS: dict[str, dict[str, str]] = {
    "rus": {
        "title": "Теория",
        "task": "Задание",
        "task_intro": (
            "Попрактикуемся! Переведи следующее предложение и отправь его "
            "голосовым сообщением:"
        ),
        "hints_pending": "Готовлю подсказки...",
        "vocabulary": "Слова",
        "grammar": "Грамматика",
        "key_phrases": "Полезные фразы",
        "notice": "Обрати внимание",
        "notes": "Как собрать фразу",
        "heard": "Вот что я услышал:",
        "correct": "✅ Верно",
        "almost": "🟡 Почти",
        "incorrect": "🔴 Не совсем",
    },
    "eng": {
        "title": "Theory",
        "task": "Task",
        "task_intro": (
            "Let's practice! Translate the following sentence and send it as a "
            "voice message:"
        ),
        "hints_pending": "Preparing hints...",
        "vocabulary": "Vocabulary",
        "grammar": "Grammar",
        "key_phrases": "Key phrases",
        "notice": "Notice",
        "notes": "Putting it together",
        "heard": "Here's what I heard:",
        "correct": "✅ Correct",
        "almost": "🟡 Almost",
        "incorrect": "🔴 Not quite",
    },
    "ukr": {
        "title": "Теорія",
        "task": "Завдання",
        "task_intro": (
            "Попрактикуємось! Перекладіть наступне речення та надішліть його "
            "голосовим повідомленням:"
        ),
        "hints_pending": "Готую підказки...",
        "vocabulary": "Слова",
        "grammar": "Граматика",
        "key_phrases": "Корисні фрази",
        "notice": "Зверни увагу",
        "notes": "Як скласти фразу",
        "heard": "Ось що я почув:",
        "correct": "✅ Правильно",
        "almost": "🟡 Майже",
        "incorrect": "🔴 Не зовсім",
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


def _bounded_list(value: object, limit: int) -> list:
    """Return at most ``limit`` items, or an empty list for malformed payloads."""
    return value[:limit] if isinstance(value, list) else []


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


def generate_sentence(
    language: str,
    native_language: str,
    difficulty: str | None = None,
    vocabulary_hint: list[str] | None = None,
    client: OpenAI | None = None,
) -> str:
    """Generate only the sentence to translate.

    Kept separate from the theory so the learner can start working on the task
    while the (much slower) theory is still being written.

    ``vocabulary_hint`` carries a few words from a patch the learner just heard
    so the exercise can reuse them. The patch text itself is never passed in:
    the learner already knows its translation, so an exercise built from it
    would be a memory test instead of a speaking one.
    """
    client = _client(client)
    target_name = _target_name(language)
    native_name = _native_name(native_language)

    parts = [
        f"Target language: {target_name} ({language})",
        f"Native language: {native_name} ({native_language})",
        f"Level: {difficulty or 'mixed'}",
        f"Theme: {random.choice(THEMES)}",
    ]
    words = [word for word in (vocabulary_hint or []) if word.strip()][:MAX_VOCABULARY_ITEMS]
    if words:
        parts.append(
            "Words the learner met recently — reuse one or two of them in a brand-new "
            "sentence about the theme above: " + ", ".join(words)
        )
    parts.append(
        f"Write the source sentence in {native_name}; the learner will translate it into "
        f"{target_name}."
    )

    resp = client.chat.completions.create(
        model=settings.openai_exercise_model,
        messages=[
            {"role": "system", "content": _SENTENCE_SYSTEM},
            {"role": "user", "content": "\n".join(parts)},
        ],
        response_format={"type": "json_object"},
        temperature=0.8,
    )
    payload = json.loads(resp.choices[0].message.content or "{}")
    return _clip(payload.get("source_sentence", ""))


def generate_theory(
    language: str,
    native_language: str,
    source_sentence: str,
    difficulty: str | None = None,
    client: OpenAI | None = None,
) -> dict:
    """Generate the theory needed to translate ``source_sentence``."""
    client = _client(client)
    target_name = _target_name(language)
    native_name = _native_name(native_language)

    parts = [
        f"Target language: {target_name} ({language})",
        f"Native language: {native_name} ({native_language})",
        f"Level: {difficulty or 'mixed'}",
        f"Source sentence ({native_name}): {source_sentence}",
        (
            f"Give the theory needed to translate it into {target_name}: {target_name} "
            f"vocabulary in dictionary form with its {native_name} meaning, full "
            "conjugation/declension tables for the forms involved, and short notes on word "
            "order. Do not reveal the translated sentence."
        ),
    ]

    resp = client.chat.completions.create(
        model=settings.openai_exercise_model,
        messages=[
            {"role": "system", "content": _THEORY_SYSTEM},
            {"role": "user", "content": "\n".join(parts)},
        ],
        response_format={"type": "json_object"},
        temperature=0.8,
    )
    payload = json.loads(resp.choices[0].message.content or "{}")
    vocabulary_items = _bounded_list(payload.get("vocabulary"), MAX_VOCABULARY_ITEMS)
    grammar_items = _bounded_list(payload.get("grammar"), MAX_GRAMMAR_TABLES)
    phrase_items = _bounded_list(payload.get("key_phrases"), MAX_KEY_PHRASES)
    note_items = _bounded_list(payload.get("notes"), MAX_NOTES)
    grammar = [
        _table(item)
        for item in grammar_items
        if isinstance(item, dict)
    ]
    key_phrases = [
        phrase
        for item in phrase_items
        if (phrase := _key_phrase(item)) is not None
    ]
    return {
        "source_sentence": _clip(source_sentence),
        "vocabulary": [
            {
                "word": _clip(item.get("word", "")),
                "translation": _clip(item.get("translation", "")),
                "note": _clip(item.get("note", "")),
            }
            for item in vocabulary_items
            if isinstance(item, dict) and str(item.get("word", "")).strip()
        ],
        "grammar": [table for table in grammar if table["rows"]],
        "key_phrases": key_phrases,
        "notes": [_clip(note) for note in note_items if str(note).strip()],
        "language": language,
        "native_language": native_language,
    }


def _details(summary: str, body: str) -> str:
    """One collapsible section; Telegram keeps it closed until tapped."""
    return f"<details><summary>{summary}</summary>{body}</details>"


def _rich_table(headers: list[str], rows: list[list[str]]) -> str:
    head = (
        "<tr>" + "".join(f"<th>{escape(cell)}</th>" for cell in headers) + "</tr>"
        if headers
        else ""
    )
    body = "".join(
        "<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table bordered striped>{head}{body}</table>"


def _vocabulary_section(exercise: dict, labels: dict[str, str]) -> str:
    """Visible-by-default vocabulary table — the first thing the learner sees."""
    rows = [
        [item["word"], item.get("translation", ""), item.get("note", "")]
        for item in exercise.get("vocabulary", [])
    ]
    if not rows:
        return ""
    return f"<h3>{escape(labels['vocabulary'])}</h3>" + _rich_table([], rows)


def _grammar_sections(exercise: dict, labels: dict[str, str]) -> list[str]:
    """One collapsible block per grammar table, so hints open one at a time."""
    sections: list[str] = []
    for raw_table in exercise.get("grammar", []):
        table = _table(raw_table)
        if not table["rows"]:
            continue
        summary = escape(table["title"] or labels["grammar"])
        body = (
            f"<p>{escape(table['explanation'])}</p>" if table["explanation"] else ""
        ) + _rich_table(table["headers"], table["rows"])
        if table["notice"]:
            body += (
                f"<p>⚠️ <b>{escape(labels['notice'])}:</b> {escape(table['notice'])}</p>"
            )
        example = table["example"]
        if example["target"] and example["native"]:
            body += (
                f"<blockquote><b>{escape(example['target'])}</b> — "
                f"{escape(example['native'])}</blockquote>"
            )
        sections.append(_details(summary, body))
    return sections


def _phrases_section(exercise: dict, labels: dict[str, str], target_name: str, native_name: str) -> str:
    rows = [
        [phrase["target"], phrase["native"]]
        for item in exercise.get("key_phrases", [])
        if (phrase := _key_phrase(item)) is not None
    ]
    if not rows:
        return ""
    table = _rich_table([target_name, native_name], rows)
    return _details(escape(labels["key_phrases"]), table)


def _notes_section(exercise: dict, labels: dict[str, str]) -> str:
    notes = exercise.get("notes", [])
    if not notes:
        return ""
    items = "".join(f"<li>{escape(note)}</li>" for note in notes)
    return _details(escape(labels["notes"]), f"<ul>{items}</ul>")


def hints_pending_text(native_language: str) -> str:
    """Placeholder shown while the theory is still being generated."""
    return _labels(native_language)["hints_pending"]


def _task_block(source_sentence: str, labels: dict[str, str]) -> str:
    return (
        f"<p>{escape(labels['task_intro'])}</p>"
        f"<p><i>{escape(source_sentence)}</i></p>"
    )


def build_task_rich_html(source_sentence: str, native_language: str) -> str:
    """Render just the task as rich-message HTML.

    Sent as a rich message from the start so the theory can be folded into the
    very same message once it is ready.
    """
    return _task_block(source_sentence, _labels(native_language))


def build_task_fallback_html(source_sentence: str, native_language: str) -> str:
    """Render just the task with classic message formatting."""
    labels = _labels(native_language)
    return f"{escape(labels['task_intro'])}\n\n<i>{escape(source_sentence)}</i>"


def build_exercise_rich_html(exercise: dict) -> str:
    """Render the task plus theory as rich-message HTML for ``sendRichMessage``.

    The vocabulary table is expanded; every other hint is a separate
    ``<details>`` block the learner can open on its own.
    """
    language = exercise["language"]
    native_language = exercise["native_language"]
    target_name = LANGUAGES[language].name if language in LANGUAGES else _target_name(language)
    native_name = _native_name(native_language)
    labels = _labels(native_language)

    header = (
        _task_block(exercise.get("source_sentence", ""), labels)
        + f"<h2>📘 {escape(labels['title'])}</h2>"
    )
    sections = [
        _vocabulary_section(exercise, labels),
        *_grammar_sections(exercise, labels),
        _phrases_section(exercise, labels, target_name, native_name),
        _notes_section(exercise, labels),
    ]
    sections = [section for section in sections if section]

    html = header + "".join(sections)
    while len(html) > MAX_RICH_CHARS and sections:
        log.warning("Practice theory exceeded %d chars; dropping a section.", MAX_RICH_CHARS)
        sections.pop()
        html = header + "".join(sections)
    return html


def build_exercise_fallback_html(exercise: dict) -> str:
    """Render the task plus theory with classic message formatting.

    Used when the rich message is rejected: no tables or per-section
    collapsing, but every block after the vocabulary is an expandable
    blockquote, so the message still stays short.
    """
    labels = _labels(exercise["native_language"])

    lines = [
        build_task_fallback_html(
            exercise.get("source_sentence", ""), exercise["native_language"]
        ),
        f"\n📘 <b>{escape(labels['title'])}</b>",
    ]
    vocabulary = exercise.get("vocabulary", [])
    if vocabulary:
        lines.append(f"\n<b>{escape(labels['vocabulary'])}</b>")
        lines += [
            f"• <b>{escape(item['word'])}</b> — {escape(item.get('translation', ''))}"
            for item in vocabulary
        ]

    def _quote(title: str, body: list[str]) -> str:
        return (
            f"\n<blockquote expandable><b>{escape(title)}</b>\n"
            + "\n".join(body)
            + "</blockquote>"
        )

    for raw_table in exercise.get("grammar", []):
        table = _table(raw_table)
        if not table["rows"]:
            continue
        body = [escape(table["explanation"])] if table["explanation"] else []
        body += [" — ".join(escape(cell) for cell in row if cell) for row in table["rows"]]
        if table["notice"]:
            body.append(f"⚠️ {escape(table['notice'])}")
        lines.append(_quote(table["title"] or labels["grammar"], body))

    phrases = [
        f"<b>{escape(phrase['target'])}</b> — {escape(phrase['native'])}"
        for item in exercise.get("key_phrases", [])
        if (phrase := _key_phrase(item)) is not None
    ]
    if phrases:
        lines.append(_quote(labels["key_phrases"], phrases))

    notes = [escape(note) for note in exercise.get("notes", [])]
    if notes:
        lines.append(_quote(labels["notes"], notes))

    return "\n".join(lines)


def transcribe_voice(audio_path: Path, language: str | None = None) -> str | None:
    """Transcribe a voice file using ElevenLabs Scribe.

    ``language`` is only a hint; leave it out to let Scribe detect the language,
    which matters because a learner speaks the target language when attempting
    the exercise but their native one when asking about it.

    Returns the transcribed text, or None when the model returns empty text.
    Raises on API errors so the caller can decide what to tell the user.
    """
    api_key = settings.elevenlabs_stt_api_key or settings.elevenlabs_api_key
    key_source = "dedicated STT key" if settings.elevenlabs_stt_api_key else "main ElevenLabs key"
    if not api_key:
        raise STTConfigurationError(
            "Neither ELEVENLABS_STT_API_KEY nor ELEVENLABS_API_KEY is set."
        )

    data: dict[str, str] = {"model_id": "scribe_v1"}
    code = ISO_639_1.get(language) if language else None
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


def _chat_messages(system: str, turns: list[dict[str, str]]) -> list[dict[str, str]]:
    """Turn the stored conversation into OpenAI chat messages.

    Learner turns keep their ``[voice]``/``[text]`` label so the tutor knows
    whether it is reading a transcript or something typed.
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for turn in turns[-MAX_HISTORY_TURNS:]:
        text = str(turn.get("text", "")).strip()
        if not text:
            continue
        if turn.get("role") == "tutor":
            messages.append({"role": "assistant", "content": text})
        else:
            kind = "voice" if turn.get("kind") == "voice" else "text"
            messages.append({"role": "user", "content": f"[{kind}] {text}"})
    return messages


def respond(
    source_sentence: str,
    turns: list[dict[str, str]],
    language: str,
    native_language: str,
    client: OpenAI | None = None,
) -> dict[str, str]:
    """Answer the learner's latest turn inside the running practice session.

    ``turns`` is the whole conversation of this exercise, oldest first, each
    item ``{"role": "learner"|"tutor", "kind": "voice"|"text", "text": ...}``.
    Returns ``{"verdict": ..., "reply": ...}`` where the verdict is ``"none"``
    unless the latest turn was an attempt at the exercise.
    """
    client = _client(client)
    system = _CHAT_TEMPLATE.format(
        native_name=_native_name(native_language),
        target_name=_target_name(language),
        source_sentence=source_sentence,
    )
    resp = client.chat.completions.create(
        model=settings.openai_exercise_model,
        messages=_chat_messages(system, turns),
        response_format={"type": "json_object"},
        temperature=0.5,
    )
    payload = json.loads(resp.choices[0].message.content or "{}")
    verdict = str(payload.get("verdict", "none")).strip().lower()
    if verdict not in VERDICTS:
        verdict = "none"
    return {"verdict": verdict, "reply": str(payload.get("reply", "")).strip()}


def build_reply_html(
    result: dict[str, str],
    native_language: str,
    transcription: str | None = None,
) -> str:
    """Render the tutor's answer, echoing the transcript of a voice turn."""
    labels = _labels(native_language)
    blocks: list[str] = []
    verdict = result.get("verdict", "none")
    if verdict in ("correct", "almost", "incorrect"):
        blocks.append(f"<b>{escape(labels[verdict])}</b>")
    if transcription:
        blocks.append(f"{escape(labels['heard'])}\n<i>{escape(transcription)}</i>")
    reply = result.get("reply", "")
    if reply:
        blocks.append(escape(reply))
    return "\n\n".join(blocks)
