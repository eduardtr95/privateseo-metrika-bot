from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


UTC = timezone.utc


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    chat_id INTEGER PRIMARY KEY,
                    username TEXT,
                    created_at TEXT NOT NULL,
                    report_enabled INTEGER NOT NULL DEFAULT 1,
                    last_report_key TEXT
                );
                CREATE TABLE IF NOT EXISTS connections (
                    chat_id INTEGER PRIMARY KEY REFERENCES users(chat_id) ON DELETE CASCADE,
                    access_token TEXT NOT NULL,
                    refresh_token TEXT,
                    expires_at TEXT,
                    counter_id INTEGER,
                    counter_name TEXT,
                    goal_ids TEXT NOT NULL DEFAULT '[]',
                    connected_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state_hash TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
                    code_verifier TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER,
                    event TEXT NOT NULL,
                    details TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def upsert_user(self, chat_id: int, username: str | None) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO users(chat_id, username, created_at) VALUES (?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET username = excluded.username""",
                (chat_id, username, self._now()),
            )

    def save_oauth_state(self, state: str, chat_id: int, verifier: str) -> None:
        expires = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        digest = hashlib.sha256(state.encode()).hexdigest()
        with self.connect() as conn:
            conn.execute("DELETE FROM oauth_states WHERE expires_at < ?", (self._now(),))
            conn.execute(
                "INSERT INTO oauth_states(state_hash, chat_id, code_verifier, expires_at) VALUES (?, ?, ?, ?)",
                (digest, chat_id, verifier, expires),
            )

    def consume_oauth_state(self, state: str) -> sqlite3.Row | None:
        digest = hashlib.sha256(state.encode()).hexdigest()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM oauth_states WHERE state_hash = ? AND expires_at >= ?",
                (digest, self._now()),
            ).fetchone()
            conn.execute("DELETE FROM oauth_states WHERE state_hash = ?", (digest,))
            return row

    def save_tokens(
        self,
        chat_id: int,
        access_token: str,
        refresh_token: str | None,
        expires_at: str | None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO connections(
                    chat_id, access_token, refresh_token, expires_at, connected_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    expires_at = excluded.expires_at,
                    counter_id = NULL,
                    counter_name = NULL,
                    goal_ids = '[]',
                    connected_at = excluded.connected_at""",
                (chat_id, access_token, refresh_token, expires_at, self._now()),
            )

    def update_tokens(
        self, chat_id: int, access_token: str, refresh_token: str | None, expires_at: str | None
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE connections SET access_token = ?, refresh_token = ?, expires_at = ? WHERE chat_id = ?",
                (access_token, refresh_token, expires_at, chat_id),
            )

    def get_connection(self, chat_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM connections WHERE chat_id = ?", (chat_id,)
            ).fetchone()

    def select_counter(self, chat_id: int, counter_id: int, name: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE connections SET counter_id = ?, counter_name = ?, goal_ids = '[]' WHERE chat_id = ?",
                (counter_id, name, chat_id),
            )

    def set_goals(self, chat_id: int, goal_ids: list[int]) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE connections SET goal_ids = ? WHERE chat_id = ?",
                (json.dumps(sorted(set(goal_ids))), chat_id),
            )

    def disconnect(self, chat_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM connections WHERE chat_id = ?", (chat_id,))
            self._event_conn(conn, chat_id, "disconnect", None)

    def delete_user(self, chat_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))

    def toggle_reports(self, chat_id: int, enabled: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET report_enabled = ? WHERE chat_id = ?", (int(enabled), chat_id)
            )

    def due_users(self, report_key: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT u.chat_id, c.* FROM users u
                JOIN connections c USING(chat_id)
                WHERE u.report_enabled = 1 AND c.counter_id IS NOT NULL
                  AND COALESCE(u.last_report_key, '') != ?""",
                (report_key,),
            ).fetchall()

    def mark_report_sent(self, chat_id: int, report_key: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET last_report_key = ? WHERE chat_id = ?", (report_key, chat_id)
            )

    def event(self, chat_id: int | None, name: str, details: str | None = None) -> None:
        with self.connect() as conn:
            self._event_conn(conn, chat_id, name, details)

    def _event_conn(
        self, conn: sqlite3.Connection, chat_id: int | None, name: str, details: str | None
    ) -> None:
        conn.execute(
            "INSERT INTO events(chat_id, event, details, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, name, details, self._now()),
        )
