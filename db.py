"""SQLite persistence layer for lingua_patch.

SQLite is kept synchronous but thread-safe: every call opens a short-lived
connection (``check_same_thread`` is irrelevant since connections are never
shared across threads) guarded by a process-wide lock. This is more than fast
enough for a low-traffic daily bot and never blocks the aiogram event loop for
any meaningful time.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from config import settings

_LOCK = threading.Lock()


@contextmanager
def _connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    path = db_path or settings.db_path
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with _LOCK:
            yield conn
            conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path | None = None) -> None:
    with _connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id            INTEGER PRIMARY KEY,
                join_date          TEXT    NOT NULL,
                is_active          INTEGER NOT NULL DEFAULT 1,
                language           TEXT    NOT NULL,
                native_language    TEXT    NOT NULL,
                personal_prompt    TEXT,
                rules_version      INTEGER NOT NULL DEFAULT 0,
                pending_prompt     TEXT,
                send_time          TEXT,
                awaiting_time      INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS content_pool (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                language        TEXT    NOT NULL,
                native_language TEXT    NOT NULL,
                audio_path      TEXT    NOT NULL,
                transcript      TEXT    NOT NULL,
                translation     TEXT,
                vocabulary_json TEXT    NOT NULL DEFAULT '[]',
                source          TEXT    NOT NULL DEFAULT 'elevenlabs',
                attribution     TEXT,
                used_count      INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT    NOT NULL,
                owner_user_id   INTEGER,
                rules_version   INTEGER
            );

            CREATE TABLE IF NOT EXISTS sent_history (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL,
                content_id INTEGER NOT NULL,
                sent_at    TEXT    NOT NULL,
                FOREIGN KEY (user_id)    REFERENCES users(user_id),
                FOREIGN KEY (content_id) REFERENCES content_pool(id)
            );

            CREATE TABLE IF NOT EXISTS meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_content_language ON content_pool(language);
            CREATE INDEX IF NOT EXISTS idx_sent_user ON sent_history(user_id);
            """
        )
        # Migration: drop legacy columns from earlier versions (e.g. tatoeba_id).
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(content_pool)")}
        if "tatoeba_id" in cols or "length" in cols:
            # SQLite doesn't support DROP COLUMN before 3.35; recreate the table.
            _migrate_drop_legacy_columns(conn)

        # Migration: add personalization / scheduling columns to existing DBs.
        _add_missing_columns(
            conn,
            "users",
            {
                "personal_prompt": "TEXT",
                "rules_version": "INTEGER NOT NULL DEFAULT 0",
                "pending_prompt": "TEXT",
                "send_time": "TEXT",
                "awaiting_time": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        _add_missing_columns(
            conn,
            "content_pool",
            {"owner_user_id": "INTEGER", "rules_version": "INTEGER"},
        )
        # Created after the migration above so it works on pre-existing DBs whose
        # content_pool did not yet have the owner_user_id column.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_content_owner ON content_pool(owner_user_id)"
        )


def _add_missing_columns(
    conn: sqlite3.Connection, table: str, columns: dict[str, str]
) -> None:
    """Add any of ``columns`` (name -> SQL type/decl) missing from ``table``."""
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, decl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _migrate_drop_legacy_columns(conn: sqlite3.Connection) -> None:
    """Drop legacy columns (tatoeba_id, length) from content_pool if present."""
    conn.executescript(
        """
        PRAGMA foreign_keys = OFF;

        DROP TABLE IF EXISTS content_pool_new;

        CREATE TABLE content_pool_new (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            language        TEXT    NOT NULL,
            native_language TEXT    NOT NULL,
            audio_path      TEXT    NOT NULL,
            transcript      TEXT    NOT NULL,
            translation     TEXT,
            vocabulary_json TEXT    NOT NULL DEFAULT '[]',
            source          TEXT    NOT NULL DEFAULT 'elevenlabs',
            attribution     TEXT,
            used_count      INTEGER NOT NULL DEFAULT 0,
            created_at      TEXT    NOT NULL
        );

        INSERT INTO content_pool_new
            (id, language, native_language, audio_path, transcript, translation,
             vocabulary_json, source, attribution, used_count, created_at)
        SELECT id, language, native_language, audio_path, transcript, translation,
               vocabulary_json, source, attribution, used_count, created_at
        FROM content_pool;

        DROP TABLE content_pool;
        ALTER TABLE content_pool_new RENAME TO content_pool;

        CREATE INDEX IF NOT EXISTS idx_content_language ON content_pool(language);

        PRAGMA foreign_keys = ON;
        """
    )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Meta (small key/value store for bot state, e.g. last daily-send date)
# --------------------------------------------------------------------------- #
def get_meta(key: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
def upsert_user(user_id: int) -> None:
    """Register a user (or reactivate one who previously blocked the bot)."""
    with _connect() as conn:
        existing = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if existing:
            conn.execute("UPDATE users SET is_active = 1 WHERE user_id = ?", (user_id,))
        else:
            conn.execute(
                "INSERT INTO users (user_id, join_date, is_active, language, native_language) "
                "VALUES (?, ?, 1, ?, ?)",
                (user_id, _now(), settings.default_language, settings.native_language),
            )


def set_user_active(user_id: int, active: bool) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET is_active = ? WHERE user_id = ?", (1 if active else 0, user_id))


def set_user_language(user_id: int, language: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE users SET language = ? WHERE user_id = ?", (language, user_id))


def get_user(user_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_active_users() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM users WHERE is_active = 1").fetchall()
        return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Personalization (per-user prompt rules)
# --------------------------------------------------------------------------- #
def set_pending_prompt(user_id: int, prompt: str | None) -> None:
    """Store a proposed personal prompt awaiting the user's confirmation."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET pending_prompt = ? WHERE user_id = ?", (prompt, user_id)
        )


def confirm_pending_prompt(user_id: int) -> int:
    """Promote the pending prompt to the active one and bump the rules version.

    Returns the new rules_version. Clears the pending prompt. An empty/blank
    prompt resets the user back to the shared pool.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT pending_prompt, rules_version FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        pending = (row["pending_prompt"] if row else None) or None
        if pending is not None and not pending.strip():
            pending = None
        new_version = int(row["rules_version"] if row else 0) + 1
        conn.execute(
            "UPDATE users SET personal_prompt = ?, rules_version = ?, "
            "pending_prompt = NULL WHERE user_id = ?",
            (pending, new_version, user_id),
        )
        return new_version


def set_send_time(user_id: int, send_time: str | None) -> None:
    """Set a fixed daily delivery time ('HH:MM') or None for a random window."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET send_time = ?, awaiting_time = 0 WHERE user_id = ?",
            (send_time, user_id),
        )


def set_awaiting_time(user_id: int, awaiting: bool) -> None:
    """Flag that the next free-text message should be parsed as a custom time."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET awaiting_time = ? WHERE user_id = ?",
            (1 if awaiting else 0, user_id),
        )


def is_personalized(user: dict[str, Any]) -> bool:
    """True when the user has an active personal prompt (non-shared pool)."""
    return bool((user.get("personal_prompt") or "").strip())


# --------------------------------------------------------------------------- #
# Content
# --------------------------------------------------------------------------- #
def insert_content(
    *,
    language: str,
    native_language: str,
    audio_path: str,
    transcript: str,
    translation: str | None,
    vocabulary: list[dict[str, str]],
    source: str = "elevenlabs",
    attribution: str | None = None,
    owner_user_id: int | None = None,
    rules_version: int | None = None,
) -> int:
    """Insert a patch. ``owner_user_id`` NULL means the shared pool; otherwise it
    is a personalized patch tagged with the owner's ``rules_version``.
    """
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO content_pool "
            "(language, native_language, audio_path, transcript, translation, "
            " vocabulary_json, source, attribution, used_count, created_at, "
            " owner_user_id, rules_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
            (
                language,
                native_language,
                audio_path,
                transcript,
                translation,
                json.dumps(vocabulary, ensure_ascii=False),
                source,
                attribution,
                _now(),
                owner_user_id,
                rules_version,
            ),
        )
        return int(cur.lastrowid)


def count_content(language: str | None = None) -> int:
    if language:
        q = "SELECT COUNT(*) AS c FROM content_pool WHERE language = ?"
        params: list[str] = [language]
    else:
        q = "SELECT COUNT(*) AS c FROM content_pool"
        params = []
    with _connect() as conn:
        row = conn.execute(q, params).fetchone()
        return int(row["c"])


def _scope_clause(user_id: int, personal_version: int | None) -> tuple[str, list[Any]]:
    """Build the WHERE fragment selecting the shared pool vs. a user's buffer.

    ``personal_version is None`` -> shared pool (``owner_user_id IS NULL``);
    otherwise -> that user's patches at the given rules version.
    """
    if personal_version is None:
        return "AND owner_user_id IS NULL", []
    return "AND owner_user_id = ? AND rules_version = ?", [user_id, personal_version]


def count_unsent(user_id: int, language: str, personal_version: int | None = None) -> int:
    """How many items in ``language`` the user has not seen, scoped to the
    shared pool or the user's personalized buffer."""
    scope, extra = _scope_clause(user_id, personal_version)
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM content_pool "
            f"WHERE language = ? {scope} "
            "  AND id NOT IN (SELECT content_id FROM sent_history WHERE user_id = ?)",
            [language, *extra, user_id],
        ).fetchone()
        return int(row["c"])


def pick_unsent_content(
    user_id: int, language: str, personal_version: int | None = None
) -> dict[str, Any] | None:
    """Return a content row in ``language`` the user has not received yet.

    Scoped to the shared pool (``personal_version is None``) or the user's
    personalized buffer at ``personal_version``. Returns None when every item
    has been seen — the caller then triggers generation rather than recycling.
    """
    scope, extra = _scope_clause(user_id, personal_version)
    with _connect() as conn:
        row = conn.execute(
            f"""
            SELECT * FROM content_pool
            WHERE language = ? {scope}
              AND id NOT IN (SELECT content_id FROM sent_history WHERE user_id = ?)
            ORDER BY RANDOM()
            LIMIT 1
            """,
            [language, *extra, user_id],
        ).fetchone()
        return dict(row) if row else None


def discard_unsent_personal(user_id: int) -> list[str]:
    """Delete the user's unseen personalized patches (e.g. after a rule change).

    Returns the audio paths of the deleted rows so the caller can unlink the
    files. Rows already delivered are kept for history integrity.
    """
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, audio_path FROM content_pool "
            "WHERE owner_user_id = ? "
            "  AND id NOT IN (SELECT content_id FROM sent_history WHERE user_id = ?)",
            (user_id, user_id),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            placeholders = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM content_pool WHERE id IN ({placeholders})", ids)
        return [r["audio_path"] for r in rows]


def get_users_with_fixed_time() -> list[dict[str, Any]]:
    """Active users who chose a fixed daily delivery time ('HH:MM')."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE is_active = 1 "
            "AND send_time IS NOT NULL AND send_time != ''"
        ).fetchall()
        return [dict(r) for r in rows]


def get_random_time_users() -> list[dict[str, Any]]:
    """Active users on the randomized daily window (no fixed time set)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users WHERE is_active = 1 "
            "AND (send_time IS NULL OR send_time = '')"
        ).fetchall()
        return [dict(r) for r in rows]


def record_sent(user_id: int, content_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sent_history (user_id, content_id, sent_at) VALUES (?, ?, ?)",
            (user_id, content_id, _now()),
        )
        conn.execute("UPDATE content_pool SET used_count = used_count + 1 WHERE id = ?", (content_id,))
