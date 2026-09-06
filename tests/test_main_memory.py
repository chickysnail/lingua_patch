"""main.py's end-of-session tutor memory hook (docs/specs/001-tutor-memory.md).

Runs the async helpers directly with asyncio.run rather than pulling in
pytest-asyncio for one test file.
"""
import asyncio

import db
import main
import memory
from tests.conftest import fresh_db


def test_update_memory_saves_new_memory_after_session(tmp_path, monkeypatch):
    fresh_db(tmp_path, monkeypatch)
    db.upsert_user(1)
    monkeypatch.setattr(memory, "update_memory", lambda *a, **k: "## Level\n- beginner")
    exercise = {
        "language": "spa",
        "native_language": "rus",
        "turns": [{"role": "learner", "kind": "voice", "text": "hola"}],
    }

    asyncio.run(main._update_memory(user_id=1, exercise=exercise))

    assert db.get_tutor_memory(1, "spa") == "## Level\n- beginner"


def test_update_memory_keeps_old_memory_on_failure(tmp_path, monkeypatch):
    fresh_db(tmp_path, monkeypatch)
    db.upsert_user(1)
    db.save_tutor_memory(1, "spa", "## Level\n- beginner")

    def boom(*_a, **_k):
        raise RuntimeError("model call failed")

    monkeypatch.setattr(memory, "update_memory", boom)
    exercise = {
        "language": "spa",
        "native_language": "rus",
        "turns": [{"role": "learner", "kind": "voice", "text": "hola"}],
    }

    asyncio.run(main._update_memory(user_id=1, exercise=exercise))  # must not raise

    assert db.get_tutor_memory(1, "spa") == "## Level\n- beginner"


def test_schedule_memory_update_skips_without_active_exercise(tmp_path, monkeypatch):
    fresh_db(tmp_path, monkeypatch)
    main._memory_update_tasks.clear()

    async def run():
        main._schedule_memory_update(user_id=1)
        assert main._memory_update_tasks == set()

    asyncio.run(run())


def test_schedule_memory_update_skips_when_learner_never_answered(tmp_path, monkeypatch):
    fresh_db(tmp_path, monkeypatch)
    main._memory_update_tasks.clear()
    db.set_active_exercise(1, "hola", "spa", "rus")
    db.append_exercise_turns(1, [{"role": "tutor", "kind": "notes", "text": "some notes"}])

    async def run():
        main._schedule_memory_update(user_id=1)
        assert main._memory_update_tasks == set()

    asyncio.run(run())


def test_schedule_memory_update_runs_when_learner_answered(tmp_path, monkeypatch):
    fresh_db(tmp_path, monkeypatch)
    db.upsert_user(1)
    main._memory_update_tasks.clear()
    db.set_active_exercise(1, "hola", "spa", "rus")
    db.append_exercise_turns(1, [{"role": "learner", "kind": "voice", "text": "hola"}])
    monkeypatch.setattr(memory, "update_memory", lambda *a, **k: "## Level\n- beginner")

    async def run():
        main._schedule_memory_update(user_id=1)
        assert len(main._memory_update_tasks) == 1
        await asyncio.gather(*main._memory_update_tasks)

    asyncio.run(run())

    assert db.get_tutor_memory(1, "spa") == "## Level\n- beginner"
