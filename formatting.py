"""Build the clean, scannable text that accompanies the daily voice note."""
from __future__ import annotations

import json
from html import escape
from typing import Any

from content import youglish_url
from languages import LANGUAGES


def _vocab_list(content: dict[str, Any]) -> list[dict[str, str]]:
    raw = content.get("vocabulary_json") or "[]"
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []


def build_message(content: dict[str, Any]) -> str:
    """Return an HTML-formatted message body for a content row.

    Sent as a follow-up text message (not a caption) so it is never truncated
    by Telegram's 1024-char caption limit.
    """
    language = content["language"]
    lang = LANGUAGES.get(language)
    flag = lang.flag if lang else "🌍"
    name = lang.name if lang else language

    transcript = escape(content["transcript"])
    lines = [f"{flag} <b>Патч дня — {escape(name)}</b> (слухай 🎧)", "", transcript]

    translation = content.get("translation")
    if translation:
        lines += ["", f"<i>{escape(translation)}</i>"]

    vocab = _vocab_list(content)
    if vocab:
        lines += ["", "💡 <b>Словничок</b>"]
        for item in vocab:
            word = item.get("word", "").strip()
            if not word:
                continue
            translation_w = escape(item.get("translation", ""))
            context = item.get("context", "").strip()
            link = youglish_url(word, language)
            ctx = f" <i>({escape(context)})</i>" if context else ""
            lines.append(f"• <a href=\"{link}\">{escape(word)}</a> — {translation_w}{ctx}")

    attribution = content.get("attribution")
    if attribution:
        lines += ["", f"<a href=\"{escape(attribution)}\">audio: Tatoeba (CC-BY)</a>"]

    return "\n".join(lines)
