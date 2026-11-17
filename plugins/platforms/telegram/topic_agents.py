"""Per-topic \"last mentioned agent\" pointer store for the Telegram platform.

The Telegram adapter keeps group conversations alive under ``require_mention``
by remembering the single agent most recently @mentioned in each topic.  When
a later group message carries no @mention at all, the mention gate consults
this store: if the topic's last-mentioned agent is this profile's own bot,
the un-mentioned message is accepted as if the bot had been mentioned.

Spec (Evan, 2026-10-05): a message mentioning exactly one bot handle sets the
pointer for ``(chat_id, thread_id)``; a message mentioning two or more bot
handles leaves it unchanged.  Slash commands and replies-to-the-bot do not
move the pointer — only explicit mentions do.  The pointer never expires; it
stays until the next single mention changes it.

Storage lives in the profile's ``state.db`` (the same file as the session
table, per Evan's directive), in a small dedicated table.  Every fleet
gateway observes the same group message stream with privacy mode off and
computes the identical handle from the same message, so the four per-profile
copies converge deterministically; each gateway only ever acts when the
pointer names its own handle.
"""

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

_TABLE = "telegram_topic_agents"


def _db_path() -> Path:
    from hermes_cli.config import get_hermes_home

    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    try:
        _initialize_schema(conn)
    except Exception:
        # A PRAGMA/DDL failure after a successful connect() must not leak the
        # just-opened connection back to the caller.
        conn.close()
        raise
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state import apply_wal_with_fallback

    apply_wal_with_fallback(conn, db_label="state.db (telegram topic agents)")
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            chat_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            agent_handle TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (chat_id, thread_id)
        )
        """
    )


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection, commit/rollback on exit, and ALWAYS close it."""
    conn = _connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def remember_last_mentioned_agent(
    chat_id: str, thread_id: str, agent_handle: str
) -> None:
    """Set (or replace) the topic's sticky agent. Single atomic upsert."""
    with _transaction() as conn:
        conn.execute(
            f"""
            INSERT INTO {_TABLE} (chat_id, thread_id, agent_handle, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(chat_id, thread_id) DO UPDATE SET
                agent_handle = excluded.agent_handle,
                updated_at = excluded.updated_at
            """,
            (str(chat_id), str(thread_id), agent_handle, time.time()),
        )


def last_mentioned_agent_for(chat_id: str, thread_id: str) -> Optional[str]:
    """Return the topic's stored agent handle, or None when unset."""
    with _transaction() as conn:
        row = conn.execute(
            f"SELECT agent_handle FROM {_TABLE} WHERE chat_id = ? AND thread_id = ?",
            (str(chat_id), str(thread_id)),
        ).fetchone()
    return row[0] if row else None
