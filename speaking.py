"""Speaking exercise: generate a practice sentence plus theory, transcribe the
learner's voice, and talk them through the exercise.

Everything is one conversation with a bilingual speaker who never leaves the
topic of language learning. The notes they write before the attempt are a turn
of that conversation like any other, so the same person later grades the attempt
against what they themselves taught, and may write further notes whenever the
learner asks about something.

The sentence is generated on its own so the learner gets the task immediately;
the notes are generated afterwards and folded into the same rich message. Notes
are a short ordered list of blocks the teacher chooses freely — a block is a
title plus prose, a table and an example, in any combination — and all but one
are collapsed, so several topics cost little space. They never contain the
answer.
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
MAX_BLOCKS = 6
MAX_EXPLANATION_BLOCKS = 3
MAX_ROWS_PER_TABLE = 12
MAX_CELL_CHARS = 200
MAX_TEXT_CHARS = 600
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

# Shared by the notes written before the attempt and by any notes the teacher
# decides to write later in the conversation.
_BLOCKS_RULES = (
    "Notes are an ordered list of blocks. A block has a short warm title and, in any "
    'combination, a paragraph of prose ("text"), a small table and one example. Use a table '
    "only where a table genuinely earns its space — a conjugation or declension paradigm, a "
    "word list with meanings, two options side by side; otherwise write prose.\n"
    "- Prose, titles and table headers are in the learner's NATIVE language; words, forms and "
    "examples are in the target language. Warm, conversational tone, no academic jargon.\n"
    "- In a word list, the target-language dictionary form comes first and its native meaning "
    "second — never the other way round.\n"
    "- A conjugation table has exactly TWO columns and is never transposed: every row is "
    "[target pronoun + native gloss, target-language form], labelled like 'eu (я)', "
    "'tu (ты)', 'ele/ela (он/она)', 'nós (мы)', 'vocês (вы)', 'eles/elas (они)'. Never use "
    "the words 'единственное', 'множественное' or 'лицо' as labels. Give the tense the "
    "sentence actually needs, not the present by default. Every form must be the real form "
    "for that exact pronoun — re-check the paradigm before answering, and never repeat a form "
    "across pronouns unless the language truly shares it. Rows have as many cells as "
    "headers.\n"
    'Set "open": true on at most ONE block — it is shown expanded, so pick the one they will '
    "need first (usually the words). Every other block stays collapsed until tapped, which is "
    "why several small focused blocks are better than one long one."
)

_BLOCKS_SCHEMA = (
    '"blocks": [{"title": "...", "text": "...", "open": false, '
    '"table": {"headers": ["...", "..."], "rows": [["...", "..."]]}, '
    '"example": {"target": "...", "native": "..."}}]'
)

# The chat system prompt goes through str.format, which chokes on JSON braces.
_BLOCKS_SCHEMA_ESCAPED = _BLOCKS_SCHEMA.replace("{", "{{").replace("}", "}}")

_THEORY_SYSTEM = (
    "You are a person who grew up speaking both the learner's native language and the target "
    "language. You have just asked the learner to say one native-language sentence out loud in "
    "the target language, and now you jot down the notes you think they need.\n"
    "You decide what is worth explaining. Read the sentence, picture what actually trips up a "
    "speaker of that native language when saying this in the target language, and cover only "
    "that — a form they cannot guess, a preposition that comes with a verb, an agreement, a "
    "word order that differs. Skip whatever they can work out themselves.\n"
    "Your notes are not a specification the answer will be checked against: wherever the "
    "sentence can be said in more than one natural way, give the options together and add one "
    "line on how they differ in feel or register, so the learner is free to choose. Where you "
    "are unsure a word is the idiomatic one, say so rather than presenting it as the only "
    "choice.\n"
    "NEVER write the full target-language translation of the sentence, and never lay the notes "
    "out as a phrase-by-phrase gloss of it (a block per chunk of the sentence, each one giving "
    "that chunk translated, is the same as handing over the answer). Titles name what is being "
    "taught — a word, a form, a rule — not a fragment of the sentence, and examples illustrate "
    "it on something OTHER than the sentence itself. The learner must still assemble it.\n"
    "So for a sentence about smelling fresh bread and giving in to a night snack, blocks whose "
    "titles are 'smelled the smell', 'fresh bread', 'night snack' are exactly wrong — that is "
    "the sentence handed back in pieces. Blocks along the lines of 'what follows sentir', "
    "'the adjective comes after the noun' and 'two ways to say \"could not\": pude and "
    "consegui' teach the same material and leave the sentence to the learner (write such titles "
    "in the learner's native language, of course).\n"
    + _BLOCKS_RULES
    + "\nBe brief: at most 6 blocks, 12 rows per table, ~200 characters per cell.\n"
    "Respond ONLY with JSON: {" + _BLOCKS_SCHEMA + "}"
)

MAX_HISTORY_TURNS = 20
VERDICTS = ("correct", "almost", "incorrect", "none")

_CHAT_TEMPLATE = (
    "You are a person who grew up speaking both {native_name} and {target_name} and you are "
    "sitting next to a learner during ONE speaking exercise. You were the one who set the "
    "task: say this {native_name} sentence out loud in {target_name}:\n"
    '"{source_sentence}"\n\n'
    "You see the whole conversation of this exercise. Every turn is labelled: "
    "[voice] is a speech-to-text transcript of what the learner said out loud — expect small "
    "recognition artefacts (missing punctuation, a homophone, a swallowed ending) and never "
    "build a correction on something that is obviously just misheard; [text] is typed, may be "
    "in any language and is often a question rather than an attempt; [notes] is a set of "
    "written notes YOU sent them earlier.\n"
    "Those notes are what the learner is working from, so honour them: anything they built out "
    "of the words and forms you taught is a legitimate answer. Judge whether the sentence "
    "works in {target_name} and carries the meaning, never whether it matches the wording you "
    "would have picked — if theirs is natural, it is correct even when you would say it "
    "differently, and you may mention your version as an alternative rather than a fix. Only "
    "call something wrong when it is actually wrong or would sound off to a native ear, and "
    "if that mistake came from your own notes being unclear or misleading, say so plainly.\n"
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
    "{target_name}. Address the learner informally. Keep the spoken reply under 60 words, "
    "plain sentences, no bullet lists, no headings, no emoji spam. When you invite another "
    "attempt, invite them to SAY it out loud — never suggest writing or typing it.\n"
    "You may attach fresh written notes to your reply whenever laying something out properly "
    "would help more than talking: a paradigm they keep missing, two words compared side by "
    "side, the rule behind a question they just asked. The notes arrive as a separate message "
    "under your reply, so keep the reply itself short and conversational and let the notes "
    "carry the detail instead of repeating it. Attach nothing (\"notes\": null) when a couple "
    "of spoken sentences really are enough, and never re-send notes you already sent. At most "
    "3 blocks.\n"
    + _BLOCKS_RULES
    + "\n"
    'Respond ONLY with JSON: {{"verdict": "correct" | "almost" | "incorrect" | "none", '
    '"reply": "your answer in {native_name}", "notes": null | {{'
    + _BLOCKS_SCHEMA_ESCAPED
    + "}}}} "
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
        "note": "Заметка",
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
        "note": "Note",
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
        "note": "Замітка",
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
    """Normalise one table, dropping rows that do not fit the headers."""
    if not isinstance(item, dict):
        return {"headers": [], "rows": []}
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
    return {"headers": headers, "rows": rows}


def _example(item: object) -> dict[str, str]:
    if not isinstance(item, dict):
        return {"target": "", "native": ""}
    return {
        "target": _clip(item.get("target", "")),
        "native": _clip(item.get("native", "")),
    }


def _block(item: object) -> dict | None:
    """Normalise one note block; None when nothing usable is left."""
    if not isinstance(item, dict):
        return None
    text = str(item.get("text") or "").strip()
    if len(text) > MAX_TEXT_CHARS:
        text = text[: MAX_TEXT_CHARS - 1].rstrip() + "…"
    block = {
        "title": _clip(item.get("title", "")),
        "text": text,
        "open": bool(item.get("open")),
        "table": _table(item.get("table")),
        "example": _example(item.get("example")),
    }
    if not block["text"] and not block["table"]["rows"] and not block["example"]["target"]:
        return None
    return block


def _blocks(payload: object, limit: int = MAX_BLOCKS) -> list[dict]:
    """Normalise the block list of a notes payload, keeping one block open."""
    items = payload.get("blocks") if isinstance(payload, dict) else None
    blocks = [
        block
        for item in _bounded_list(items, limit)
        if (block := _block(item)) is not None
    ]
    for block in blocks[1:]:
        block["open"] = False
    return blocks


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
    """Write the notes the learner needs to say ``source_sentence`` themselves.

    The teacher chooses the blocks; nothing here dictates which topics appear.
    """
    client = _client(client)
    target_name = _target_name(language)
    native_name = _native_name(native_language)

    parts = [
        f"Target language: {target_name} ({language})",
        f"Native language: {native_name} ({native_language})",
        f"Level: {difficulty or 'mixed'}",
        f"Source sentence ({native_name}): {source_sentence}",
        (
            f"Write your notes for saying it in {target_name}. Cover only what this learner "
            "actually needs; show the choices where more than one wording is natural. Do not "
            "reveal the translated sentence."
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
    return {
        "source_sentence": _clip(source_sentence),
        "blocks": _blocks(payload),
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


def _block_body(block: dict) -> str:
    """The prose, table and example of one block as rich-message HTML."""
    body = f"<p>{escape(block['text'])}</p>" if block["text"] else ""
    table = block["table"]
    if table["rows"]:
        body += _rich_table(table["headers"], table["rows"])
    example = block["example"]
    if example["target"]:
        native = f" — {escape(example['native'])}" if example["native"] else ""
        body += f"<blockquote><b>{escape(example['target'])}</b>{native}</blockquote>"
    return body


def _block_sections(blocks: list[dict], labels: dict[str, str]) -> list[str]:
    """Render blocks, collapsing every one the teacher did not mark as open."""
    sections: list[str] = []
    for block in blocks:
        title = escape(block["title"] or labels["note"])
        body = _block_body(block)
        if not body:
            continue
        sections.append(
            f"<h3>{title}</h3>{body}" if block["open"] else _details(title, body)
        )
    return sections


def _fit(header: str, sections: list[str]) -> str:
    """Join sections under ``header``, dropping trailing ones that do not fit."""
    html = header + "".join(sections)
    while len(html) > MAX_RICH_CHARS and sections:
        log.warning("Practice notes exceeded %d chars; dropping a block.", MAX_RICH_CHARS)
        sections.pop()
        html = header + "".join(sections)
    return html


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
    """Render the task plus the notes as rich-message HTML for ``sendRichMessage``.

    The one block the teacher marked as open is expanded; the rest are separate
    ``<details>`` blocks the learner opens on their own.
    """
    labels = _labels(exercise["native_language"])
    header = (
        _task_block(exercise.get("source_sentence", ""), labels)
        + f"<h2>📘 {escape(labels['title'])}</h2>"
    )
    return _fit(header, _block_sections(exercise.get("blocks", []), labels))


def build_notes_rich_html(notes: dict, native_language: str) -> str:
    """Render standalone notes the teacher wrote during the conversation."""
    labels = _labels(native_language)
    return _fit("", _block_sections(notes.get("blocks", []), labels))


def _fallback_blocks(blocks: list[dict], labels: dict[str, str]) -> list[str]:
    """Blocks with classic formatting: an expandable blockquote each."""
    lines: list[str] = []
    for block in blocks:
        body = [escape(block["text"])] if block["text"] else []
        body += [
            " — ".join(escape(cell) for cell in row if cell)
            for row in block["table"]["rows"]
        ]
        example = block["example"]
        if example["target"]:
            native = f" — {escape(example['native'])}" if example["native"] else ""
            body.append(f"<b>{escape(example['target'])}</b>{native}")
        if not body:
            continue
        title = escape(block["title"] or labels["note"])
        lines.append(
            f"\n<blockquote expandable><b>{title}</b>\n" + "\n".join(body) + "</blockquote>"
        )
    return lines


def build_exercise_fallback_html(exercise: dict) -> str:
    """Render the task plus the notes with classic message formatting.

    Used when the rich message is rejected: no tables, but every block is an
    expandable blockquote, so the message still stays short.
    """
    labels = _labels(exercise["native_language"])
    lines = [
        build_task_fallback_html(
            exercise.get("source_sentence", ""), exercise["native_language"]
        ),
        f"\n📘 <b>{escape(labels['title'])}</b>",
        *_fallback_blocks(exercise.get("blocks", []), labels),
    ]
    return "\n".join(lines)


def build_notes_fallback_html(notes: dict, native_language: str) -> str:
    """Standalone conversation notes with classic message formatting."""
    labels = _labels(native_language)
    lines = _fallback_blocks(notes.get("blocks", []), labels)
    return "\n".join(lines).lstrip("\n")


def notes_to_text(notes: dict, native_language: str | None = None) -> str:
    """Flatten notes into plain text so they can live in the conversation.

    This is what the teacher sees on the next turn, so it must contain
    everything they taught — titles, prose, every table row and the examples.
    """
    labels = _labels(native_language or notes.get("native_language", "eng"))
    parts: list[str] = []
    for block in notes.get("blocks", []):
        lines = [block["title"] or labels["note"]]
        if block["text"]:
            lines.append(block["text"])
        lines += [
            " — ".join(cell for cell in row if cell) for row in block["table"]["rows"]
        ]
        example = block["example"]
        if example["target"]:
            lines.append(f"{example['target']} — {example['native']}".rstrip(" —"))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


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
    whether it is reading a transcript or something typed, and the notes the
    tutor sent are labelled ``[notes]`` so it grades against what it taught.
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": system}]
    for turn in turns[-MAX_HISTORY_TURNS:]:
        text = str(turn.get("text", "")).strip()
        if not text:
            continue
        if turn.get("role") == "tutor":
            prefix = "[notes]\n" if turn.get("kind") == "notes" else ""
            messages.append({"role": "assistant", "content": prefix + text})
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
) -> dict:
    """Answer the learner's latest turn inside the running practice session.

    ``turns`` is the whole conversation of this exercise, oldest first, each
    item ``{"role": "learner"|"tutor", "kind": "voice"|"text"|"notes",
    "text": ...}`` — including the notes sent with the task, so the answer is
    judged against what this teacher actually taught.

    Returns ``{"verdict": ..., "reply": ..., "notes": ...}``: the verdict is
    ``"none"`` unless the latest turn was an attempt, and ``notes`` carries
    extra written notes the teacher chose to attach (empty blocks otherwise).
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
    return {
        "verdict": verdict,
        "reply": str(payload.get("reply", "")).strip(),
        "notes": {"blocks": _blocks(payload.get("notes"), MAX_EXPLANATION_BLOCKS)},
    }


def build_reply_html(
    result: dict,
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
