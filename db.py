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
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
                user_id         INTEGER PRIMARY KEY,
                join_date       TEXT    NOT NULL,
                is_active       INTEGER NOT NULL DEFAULT 1,
                language        TEXT    NOT NULL,
                native_language TEXT    NOT NULL,
                difficulty      TEXT,
                send_time       TEXT,
                awaiting_time   INTEGER NOT NULL DEFAULT 0
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
                difficulty      TEXT
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

        # Migration: add the difficulty + delivery-time columns to pre-existing DBs.
        _add_missing_columns(
            conn,
            "users",
            {
                "difficulty": "TEXT",
                "send_time": "TEXT",
                "awaiting_time": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        _add_missing_columns(conn, "content_pool", {"difficulty": "TEXT"})
        # Created after the migration so it also works on DBs whose content_pool
        # did not yet have the difficulty column.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_content_difficulty ON content_pool(difficulty)"
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


def set_user_difficulty(user_id: int, difficulty: str | None) -> None:
    """Set the learner's difficulty level, or None for the default (unmarked) pool."""
    with _connect() as conn:
        conn.execute(
            "UPDATE users SET difficulty = ? WHERE user_id = ?", (difficulty, user_id)
        )


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


def get_user(user_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def get_active_users() -> list[dict[str, Any]]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM users WHERE is_active = 1").fetchall()
        return [dict(r) for r in rows]


def get_all_users_with_stats() -> list[dict[str, Any]]:
    """Every user plus their delivery counters, most recently active first."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT u.*,
                   COUNT(s.id)   AS patches_sent,
                   MAX(s.sent_at) AS last_sent
            FROM users u
            LEFT JOIN sent_history s ON s.user_id = u.user_id
            GROUP BY u.user_id
            ORDER BY last_sent IS NULL, last_sent DESC, u.join_date DESC
            """
        ).fetchall()
        return [dict(r) for r in rows]


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
    difficulty: str | None = None,
) -> int:
    """Insert a patch. ``difficulty`` NULL means the default (unmarked) pool."""
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO content_pool "
            "(language, native_language, audio_path, transcript, translation, "
            " vocabulary_json, source, attribution, used_count, created_at, difficulty) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
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
                difficulty,
            ),
        )
        return int(cur.lastrowid)


def _difficulty_clause(difficulty: str | None) -> tuple[str, list[Any]]:
    """WHERE fragment selecting the default (unmarked) pool vs. a difficulty tier.

    ``difficulty is None`` -> ``difficulty IS NULL`` (default pool, unchanged);
    otherwise -> ``difficulty = ?``.
    """
    if difficulty is None:
        return "AND difficulty IS NULL", []
    return "AND difficulty = ?", [difficulty]


def count_content(language: str | None = None, difficulty: str | None = None) -> int:
    clauses = []
    params: list[Any] = []
    if language:
        clauses.append("language = ?")
        params.append(language)
    if difficulty is not None:
        clauses.append("difficulty = ?")
        params.append(difficulty)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM content_pool{where}", params).fetchone()
        return int(row["c"])


def count_unsent(user_id: int, language: str, difficulty: str | None = None) -> int:
    """How many items in ``language`` at the given tier the user has not seen."""
    scope, extra = _difficulty_clause(difficulty)
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM content_pool "
            f"WHERE language = ? {scope} "
            "  AND id NOT IN (SELECT content_id FROM sent_history WHERE user_id = ?)",
            [language, *extra, user_id],
        ).fetchone()
        return int(row["c"])


def get_content(content_id: int) -> dict[str, Any] | None:
    """Return a single content pool row by id."""
    with _connect() as conn:
        row = conn.execute("SELECT * FROM content_pool WHERE id = ?", (content_id,)).fetchone()
        return dict(row) if row else None


def pick_unsent_content(
    user_id: int, language: str, difficulty: str | None = None
) -> dict[str, Any] | None:
    """Return a content row in ``language`` the user has not received yet.

    Scoped to the default unmarked pool (``difficulty is None``) or a specific
    difficulty tier. Returns None when every item has been seen — the caller
    then triggers pool expansion rather than recycling old content.
    """
    scope, extra = _difficulty_clause(difficulty)
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


def record_sent(user_id: int, content_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sent_history (user_id, content_id, sent_at) VALUES (?, ?, ?)",
            (user_id, content_id, _now()),
        )
        conn.execute("UPDATE content_pool SET used_count = used_count + 1 WHERE id = ?", (content_id,))
