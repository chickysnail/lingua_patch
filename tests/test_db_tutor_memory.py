"""tutor_memory schema and query tests (docs/specs/001-tutor-memory.md)."""
import db
from tests.conftest import fresh_db


def test_init_db_creates_tutor_memory_table_and_is_idempotent(tmp_path, monkeypatch):
    path = fresh_db(tmp_path, monkeypatch)
    db.init_db(path)  # safe to run twice, on a DB that already has the table

    with db._connect(path) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tutor_memory)")}
    assert cols == {"user_id", "language", "memory", "memory_prev", "updated_at"}


def test_get_tutor_memory_defaults_to_empty_string(tmp_path, monkeypatch):
    fresh_db(tmp_path, monkeypatch)
    assert db.get_tutor_memory(user_id=1, language="spa") == ""


def test_save_then_get_tutor_memory_roundtrips(tmp_path, monkeypatch):
    fresh_db(tmp_path, monkeypatch)
    db.upsert_user(1)
    db.save_tutor_memory(1, "spa", "## Level\n- beginner")
    assert db.get_tutor_memory(1, "spa") == "## Level\n- beginner"


def test_save_tutor_memory_keeps_previous_version(tmp_path, monkeypatch):
    fresh_db(tmp_path, monkeypatch)
    db.upsert_user(1)
    db.save_tutor_memory(1, "spa", "## Level\n- beginner")
    db.save_tutor_memory(1, "spa", "## Level\n- intermediate")

    with db._connect() as conn:
        row = conn.execute(
            "SELECT memory, memory_prev FROM tutor_memory WHERE user_id = ? AND language = ?",
            (1, "spa"),
        ).fetchone()
    assert row["memory"] == "## Level\n- intermediate"
    assert row["memory_prev"] == "## Level\n- beginner"


def test_tutor_memory_is_scoped_per_language(tmp_path, monkeypatch):
    """Switching /language must not mix a fact specific to one target language
    into another (spec: "one row per learner per target language")."""
    fresh_db(tmp_path, monkeypatch)
    db.upsert_user(1)
    db.save_tutor_memory(1, "spa", "## Recurring errors\n- confuses ser and estar")
    db.save_tutor_memory(1, "por_pt", "## Recurring errors\n- drops the definite article")

    assert "ser and estar" in db.get_tutor_memory(1, "spa")
    assert "definite article" in db.get_tutor_memory(1, "por_pt")
    assert "ser and estar" not in db.get_tutor_memory(1, "por_pt")
