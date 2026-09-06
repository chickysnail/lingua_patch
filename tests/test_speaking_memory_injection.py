"""Tutor memory reaching speaking.py's three prompts (docs/specs/001-tutor-memory.md).

No network calls: a fake OpenAI client records the messages it was asked to
send and returns a canned response.
"""
import json
from types import SimpleNamespace

import speaking


class _RecordingClient:
    """Stands in for openai.OpenAI, capturing the last request's messages."""

    def __init__(self, payload: dict):
        self.messages = None
        self._payload = payload

    @property
    def chat(self):
        return SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.messages = kwargs["messages"]
        content = json.dumps(self._payload)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_generate_sentence_identical_for_none_and_empty_memory(monkeypatch):
    monkeypatch.setattr(speaking.random, "choice", lambda seq: seq[0])

    client_none = _RecordingClient({"source_sentence": "hola"})
    speaking.generate_sentence("spa", "rus", client=client_none)

    client_empty = _RecordingClient({"source_sentence": "hola"})
    speaking.generate_sentence("spa", "rus", tutor_memory="", client=client_empty)

    assert client_none.messages == client_empty.messages


def test_generate_sentence_carries_memory_when_present(monkeypatch):
    monkeypatch.setattr(speaking.random, "choice", lambda seq: seq[0])

    client = _RecordingClient({"source_sentence": "hola"})
    speaking.generate_sentence("spa", "rus", tutor_memory="## Level\n- beginner", client=client)

    user_message = client.messages[-1]["content"]
    assert "LEARNER MEMORY" in user_message
    assert "beginner" in user_message


def test_generate_theory_identical_for_none_and_empty_memory():
    payload = {"blocks": []}
    client_none = _RecordingClient(payload)
    speaking.generate_theory("spa", "rus", "Hola, ¿cómo estás?", client=client_none)

    client_empty = _RecordingClient(payload)
    speaking.generate_theory(
        "spa", "rus", "Hola, ¿cómo estás?", tutor_memory="", client=client_empty
    )

    assert client_none.messages == client_empty.messages


def test_generate_theory_carries_memory_when_present():
    client = _RecordingClient({"blocks": []})
    speaking.generate_theory(
        "spa", "rus", "Hola", tutor_memory="## Recurring errors\n- ser vs estar", client=client
    )

    user_message = client.messages[-1]["content"]
    assert "LEARNER MEMORY" in user_message
    assert "ser vs estar" in user_message


def test_respond_identical_for_none_and_empty_memory():
    payload = {"verdict": "none", "reply": "ok", "notes": None}
    turns = [{"role": "learner", "kind": "text", "text": "hola"}]

    client_none = _RecordingClient(payload)
    speaking.respond("Hola", turns, "spa", "rus", client=client_none)

    client_empty = _RecordingClient(payload)
    speaking.respond("Hola", turns, "spa", "rus", tutor_memory="", client=client_empty)

    assert client_none.messages == client_empty.messages


def test_respond_carries_memory_when_present():
    payload = {"verdict": "none", "reply": "ok", "notes": None}
    turns = [{"role": "learner", "kind": "text", "text": "hola"}]

    client = _RecordingClient(payload)
    speaking.respond(
        "Hola", turns, "spa", "rus", tutor_memory="## About the learner\n- works in tech",
        client=client,
    )

    system_message = client.messages[0]["content"]
    assert "LEARNER MEMORY" in system_message
    assert "works in tech" in system_message
