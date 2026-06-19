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
                user_id         INTEGER PRIMARY KEY,
                join_date       TEXT    NOT NULL,
                is_active       INTEGER NOT NULL DEFAULT 1,
                language        TEXT    NOT NULL,
                native_language TEXT    NOT NULL
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
                created_at      TEXT    NOT NULL
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
        # Migration: drop legacy columns that may exist from earlier versions.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(content_pool)")}
        if "tatoeba_id" in cols:
            # SQLite doesn't support DROP COLUMN before 3.35; recreate the table.
            _migrate_drop_legacy_columns(conn)


def _migrate_drop_legacy_columns(conn: sqlite3.Connection) -> None:
    """Drop tatoeba_id and length columns from content_pool if present."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS content_pool_new (
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
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO content_pool "
            "(language, native_language, audio_path, transcript, translation, "
            " vocabulary_json, source, attribution, used_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
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


def count_unsent(user_id: int, language: str) -> int:
    """How many items in ``language`` the user has not seen."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM content_pool "
            "WHERE language = ? "
            "  AND id NOT IN (SELECT content_id FROM sent_history WHERE user_id = ?)",
            (language, user_id),
        ).fetchone()
        return int(row["c"])


def pick_unsent_content(user_id: int, language: str) -> dict[str, Any] | None:
    """Return a content row in ``language`` the user has not received yet.

    Returns None when every item has been seen — the caller is expected to
    trigger pool expansion rather than recycling old content.
    """
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM content_pool
            WHERE language = ?
              AND id NOT IN (SELECT content_id FROM sent_history WHERE user_id = ?)
            ORDER BY RANDOM()
            LIMIT 1
            """,
            (language, user_id),
        ).fetchone()
        return dict(row) if row else None


def record_sent(user_id: int, content_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sent_history (user_id, content_id, sent_at) VALUES (?, ?, ?)",
            (user_id, content_id, _now()),
        )
        conn.execute("UPDATE content_pool SET used_count = used_count + 1 WHERE id = ?", (content_id,))
