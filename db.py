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
                tatoeba_id      INTEGER UNIQUE,
                audio_path      TEXT    NOT NULL,
                transcript      TEXT    NOT NULL,
                translation     TEXT,
                vocabulary_json TEXT    NOT NULL DEFAULT '[]',
                source          TEXT    NOT NULL DEFAULT 'tatoeba',
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
        # Migration: add the 'length' dimension ('short' | 'long'). Existing rows
        # are backfilled from their source (native Tatoeba clips are short; the
        # original ElevenLabs snippets were the 2-4 sentence 'long' style).
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(content_pool)")}
        if "length" not in cols:
            conn.execute("ALTER TABLE content_pool ADD COLUMN length TEXT NOT NULL DEFAULT 'short'")
            conn.execute("UPDATE content_pool SET length = 'long' WHERE source = 'elevenlabs'")
            conn.execute("UPDATE content_pool SET length = 'short' WHERE source = 'tatoeba'")


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
def content_exists(tatoeba_id: int) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT 1 FROM content_pool WHERE tatoeba_id = ?", (tatoeba_id,)).fetchone()
        return row is not None


def insert_content(
    *,
    language: str,
    native_language: str,
    tatoeba_id: int | None,
    audio_path: str,
    transcript: str,
    translation: str | None,
    vocabulary: list[dict[str, str]],
    source: str = "tatoeba",
    attribution: str | None = None,
    length: str = "short",
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO content_pool "
            "(language, native_language, tatoeba_id, audio_path, transcript, translation, "
            " vocabulary_json, source, attribution, length, used_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (
                language,
                native_language,
                tatoeba_id,
                audio_path,
                transcript,
                translation,
                json.dumps(vocabulary, ensure_ascii=False),
                source,
                attribution,
                length,
                _now(),
            ),
        )
        return int(cur.lastrowid)


def prune_to_keep(language: str, source: str, keep: int) -> list[str]:
    """Keep at most ``keep`` items of ``source`` for ``language``; delete the rest.

    Returns the audio paths of deleted rows so the caller can unlink the files.
    Least-used items are kept. Any sent_history rows for deleted content are
    removed first to satisfy the foreign key.
    """
    if keep < 0:
        return []
    with _connect() as conn:
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM content_pool WHERE language = ? AND source = ? "
                "ORDER BY used_count ASC, id ASC",
                (language, source),
            ).fetchall()
        ]
        to_delete = ids[keep:]
        paths: list[str] = []
        for cid in to_delete:
            row = conn.execute("SELECT audio_path FROM content_pool WHERE id = ?", (cid,)).fetchone()
            if row and row["audio_path"]:
                paths.append(row["audio_path"])
            conn.execute("DELETE FROM sent_history WHERE content_id = ?", (cid,))
            conn.execute("DELETE FROM content_pool WHERE id = ?", (cid,))
        return paths


def count_content(language: str | None = None, source: str | None = None,
                   length: str | None = None) -> int:
    clauses, params = [], []
    if language:
        clauses.append("language = ?")
        params.append(language)
    if source:
        clauses.append("source = ?")
        params.append(source)
    if length:
        clauses.append("length = ?")
        params.append(length)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _connect() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM content_pool{where}", params).fetchone()
        return int(row["c"])


def count_unsent(user_id: int, language: str, length: str | None = None) -> int:
    """How many items in ``language`` (optionally of a given ``length``) the user has not seen."""
    clauses = ["language = ?", "id NOT IN (SELECT content_id FROM sent_history WHERE user_id = ?)"]
    params: list[Any] = [language, user_id]
    if length:
        clauses.append("length = ?")
        params.append(length)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS c FROM content_pool WHERE {' AND '.join(clauses)}", params
        ).fetchone()
        return int(row["c"])


def pick_unsent_content(user_id: int, language: str, length: str | None = None) -> dict[str, Any] | None:
    """Return a content row in ``language`` the user has not received yet.

    When ``length`` is given, only items of that length are considered (each
    length pool mixes native + AI items, so the audio source varies per pick).
    Falls back to the least-used matching item once the user has seen everything,
    so the bot keeps working past the end of the pool.
    """
    len_clause = " AND length = ?" if length else ""
    with _connect() as conn:
        params: list[Any] = [language, user_id]
        if length:
            params.append(length)
        row = conn.execute(
            f"""
            SELECT * FROM content_pool
            WHERE language = ?
              AND id NOT IN (SELECT content_id FROM sent_history WHERE user_id = ?)
              {len_clause}
            ORDER BY used_count ASC, RANDOM()
            LIMIT 1
            """,
            params,
        ).fetchone()
        if row is None:
            # Everything seen: recycle the least-used matching item.
            recycle_params: list[Any] = [language]
            if length:
                recycle_params.append(length)
            row = conn.execute(
                f"SELECT * FROM content_pool WHERE language = ?{len_clause} "
                "ORDER BY used_count ASC, RANDOM() LIMIT 1",
                recycle_params,
            ).fetchone()
        return dict(row) if row else None


def record_sent(user_id: int, content_id: int) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sent_history (user_id, content_id, sent_at) VALUES (?, ?, ?)",
            (user_id, content_id, _now()),
        )
        conn.execute("UPDATE content_pool SET used_count = used_count + 1 WHERE id = ?", (content_id,))
