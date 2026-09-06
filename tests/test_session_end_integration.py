"""End-to-end (minus the network) check of one full session-end update.

Exercises the real call chain — main._update_memory -> memory.update_memory ->
memory.generate_update -> memory.apply_delta -> db.save_tutor_memory — with
only the OpenAI client faked, per the spec's "Done when":
"After one completed practice session, that learner's memory is non-empty and
contains at least one populated section."
"""
import asyncio
import json
from types import SimpleNamespace

import db
import main
from tests.conftest import fresh_db


class _FakeUpdateClient:
    def __init__(self, delta: dict):
        self._delta = delta

    @property
    def chat(self):
        return SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **_kwargs):
        content = json.dumps(self._delta)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


def test_completed_session_produces_nonempty_memory(tmp_path, monkeypatch):
    fresh_db(tmp_path, monkeypatch)
    db.upsert_user(42)
    main._memory_update_tasks.clear()

    fake_client = _FakeUpdateClient(
        {"add": [{"section": "Recurring errors", "text": "mixes up ser and estar"}]}
    )
    import memory as memory_module

    monkeypatch.setattr(memory_module, "_client", lambda client: fake_client)

    db.set_active_exercise(42, "Hola, ¿cómo estás?", "spa", "rus")
    db.append_exercise_turns(
        42,
        [
            {"role": "tutor", "kind": "notes", "text": "some notes about ser/estar"},
            {"role": "learner", "kind": "voice", "text": "Hola, como estas"},
        ],
    )

    async def run():
        main._schedule_memory_update(42)
        await asyncio.gather(*main._memory_update_tasks)

    asyncio.run(run())

    stored = db.get_tutor_memory(42, "spa")
    assert stored != ""
    assert "## Recurring errors" in stored
    assert "mixes up ser and estar" in stored
