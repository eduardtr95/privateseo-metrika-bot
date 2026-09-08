from __future__ import annotations

import hashlib
import json
import sqlite3
import secrets
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
            user_columns = {
                str(row["name"]) for row in conn.execute("PRAGMA table_info(users)").fetchall()
            }
            migrations = {
                "report_frequency": "TEXT NOT NULL DEFAULT 'weekly'",
                "report_weekday": "INTEGER NOT NULL DEFAULT 0",
                "report_hour": "INTEGER NOT NULL DEFAULT 9",
                "first_start_payload": "TEXT",
                "first_started_at": "TEXT",
            }
            for name, definition in migrations.items():
                if name not in user_columns:
                    conn.execute(f"ALTER TABLE users ADD COLUMN {name} {definition}")

            for table, additions in {
                "users": {
                    "epoch": "TEXT",
                    "report_retry_at": "TEXT",
                    "report_error_notice": "TEXT",
                },
                "connections": {
                    "generation": "TEXT",
                    "reauth_required": "INTEGER NOT NULL DEFAULT 0",
                },
                "oauth_states": {"user_epoch": "TEXT"},
            }.items():
                columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
                for name, definition in additions.items():
                    if name not in columns:
                        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            conn.execute("UPDATE users SET epoch = lower(hex(randomblob(8))) WHERE epoch IS NULL")
            conn.execute(
                "UPDATE connections SET generation = lower(hex(randomblob(8))) WHERE generation IS NULL"
            )
            # Old pending OAuth URLs predate epoch binding; require a fresh URL.
            conn.execute("DELETE FROM oauth_states WHERE user_epoch IS NULL")
            conn.execute("""CREATE TABLE IF NOT EXISTS report_contexts (
                id TEXT PRIMARY KEY,
                chat_id INTEGER NOT NULL REFERENCES users(chat_id) ON DELETE CASCADE,
                generation TEXT NOT NULL, payload TEXT NOT NULL, expires_at TEXT NOT NULL
            )""")

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat()

    def upsert_user(self, chat_id: int, username: str | None) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO users(chat_id, username, created_at, epoch) VALUES (?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET username = excluded.username""",
                (chat_id, username, self._now(), secrets.token_hex(8)),
            )

    def get_user(self, chat_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()

    def record_first_start(self, chat_id: int, payload: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                """UPDATE users
                SET first_start_payload = ?, first_started_at = ?
                WHERE chat_id = ? AND first_started_at IS NULL""",
                (payload, self._now(), chat_id),
            )
            if cursor.rowcount:
                self._event_conn(conn, chat_id, "start", payload)
                return True
            return False

    def save_oauth_state(self, state: str, chat_id: int, verifier: str) -> None:
        expires = (datetime.now(UTC) + timedelta(minutes=10)).isoformat()
        digest = hashlib.sha256(state.encode()).hexdigest()
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET epoch = ? WHERE chat_id = ?", (secrets.token_hex(8), chat_id)
            )
            conn.execute(
                "DELETE FROM oauth_states WHERE expires_at < ? OR chat_id = ?",
                (self._now(), chat_id),
            )
            conn.execute(
                "INSERT INTO oauth_states(state_hash, chat_id, code_verifier, expires_at, user_epoch) SELECT ?, chat_id, ?, ?, epoch FROM users WHERE chat_id = ?",
                (digest, verifier, expires, chat_id),
            )

    def consume_oauth_state(self, state: str) -> sqlite3.Row | None:
        digest = hashlib.sha256(state.encode()).hexdigest()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
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
                    chat_id, access_token, refresh_token, expires_at, connected_at, generation
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    expires_at = excluded.expires_at,
                    counter_id = NULL,
                    counter_name = NULL,
                    goal_ids = '[]',
                    connected_at = excluded.connected_at,
                    generation = excluded.generation, reauth_required = 0""",
                (
                    chat_id,
                    access_token,
                    refresh_token,
                    expires_at,
                    self._now(),
                    secrets.token_hex(8),
                ),
            )

    def update_tokens(
        self,
        chat_id: int,
        access_token: str,
        refresh_token: str | None,
        expires_at: str | None,
        *,
        expected_generation: str | None = None,
    ) -> bool:
        with self.connect() as conn:
            cursor = conn.execute(
                "UPDATE connections SET access_token = ?, refresh_token = ?, expires_at = ? WHERE chat_id = ? AND (? IS NULL OR generation = ?)",
                (
                    access_token,
                    refresh_token,
                    expires_at,
                    chat_id,
                    expected_generation,
                    expected_generation,
                ),
            )
            return bool(cursor.rowcount)

    def get_connection(self, chat_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM connections WHERE chat_id = ?", (chat_id,)
            ).fetchone()

    def select_counter(self, chat_id: int, counter_id: int, name: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE connections SET counter_id = ?, counter_name = ?, goal_ids = '[]', generation = ?, reauth_required = 0 WHERE chat_id = ?",
                (counter_id, name, secrets.token_hex(8), chat_id),
            )

    def set_goals(self, chat_id: int, goal_ids: list[int]) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE connections SET goal_ids = ? WHERE chat_id = ?",
                (json.dumps(sorted(set(goal_ids))), chat_id),
            )

    def toggle_goal(
        self, chat_id: int, goal_id: int, limit: int = 15
    ) -> tuple[list[int], bool | None]:
        """Atomically toggle a goal. The second value is True=added, False=removed, None=limit."""
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT goal_ids FROM connections WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if not row:
                return [], None
            selected = set(json.loads(row["goal_ids"] or "[]"))
            if goal_id in selected:
                selected.remove(goal_id)
                added: bool | None = False
            elif len(selected) >= limit:
                return sorted(selected), None
            else:
                selected.add(goal_id)
                added = True
            result = sorted(selected)
            conn.execute(
                "UPDATE connections SET goal_ids = ? WHERE chat_id = ?",
                (json.dumps(result), chat_id),
            )
            return result, added

    def disconnect(self, chat_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET epoch = ? WHERE chat_id = ?", (secrets.token_hex(8), chat_id)
            )
            conn.execute("DELETE FROM oauth_states WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM report_contexts WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM connections WHERE chat_id = ?", (chat_id,))
            self._event_conn(conn, chat_id, "disconnect", None)

    def delete_user(self, chat_id: int) -> None:
        with self.connect() as conn:
            # Events intentionally have no foreign key because some system events
            # are anonymous. User-scoped events still contain the Telegram chat ID
            # and therefore must be removed explicitly.
            conn.execute("DELETE FROM events WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM users WHERE chat_id = ?", (chat_id,))

    def toggle_reports(self, chat_id: int, enabled: bool) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET report_enabled = ? WHERE chat_id = ?", (int(enabled), chat_id)
            )

    def set_report_schedule(
        self,
        chat_id: int,
        *,
        frequency: str | None = None,
        weekday: int | None = None,
        hour: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        fields: list[str] = []
        values: list[object] = []
        if frequency is not None:
            if frequency not in {"daily", "weekly"}:
                raise ValueError("Invalid report frequency")
            fields.append("report_frequency = ?")
            values.append(frequency)
        if weekday is not None:
            if weekday not in range(7):
                raise ValueError("Invalid report weekday")
            fields.append("report_weekday = ?")
            values.append(weekday)
        if hour is not None:
            if hour not in range(24):
                raise ValueError("Invalid report hour")
            fields.append("report_hour = ?")
            values.append(hour)
        if enabled is not None:
            fields.append("report_enabled = ?")
            values.append(int(enabled))
        if not fields:
            return
        values.append(chat_id)
        with self.connect() as conn:
            conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE chat_id = ?", values)

    def scheduled_users(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """SELECT u.*, c.access_token, c.refresh_token, c.expires_at,
                          c.counter_id, c.counter_name, c.goal_ids, c.connected_at, c.generation
                FROM users u JOIN connections c USING(chat_id)
                WHERE u.report_enabled = 1 AND c.counter_id IS NOT NULL AND c.reauth_required = 0
                AND (u.report_retry_at IS NULL OR u.report_retry_at <= ?) ORDER BY COALESCE(u.last_report_key, ''), u.chat_id""",
                (self._now(),),
            ).fetchall()

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
                "UPDATE users SET last_report_key = ?, report_retry_at = NULL, report_error_notice = NULL WHERE chat_id = ?",
                (report_key, chat_id),
            )

    def event(self, chat_id: int | None, name: str, details: str | None = None) -> None:
        with self.connect() as conn:
            self._event_conn(conn, chat_id, name, details)

    def _event_conn(
        self, conn: sqlite3.Connection, chat_id: int | None, name: str, details: str | None
    ) -> None:
        if (
            chat_id is not None
            and not conn.execute("SELECT 1 FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
        ):
            return
        conn.execute(
            "INSERT INTO events(chat_id, event, details, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, name, details, self._now()),
        )

    def save_report_context(self, chat_id: int, generation: str, payload: dict) -> str:
        key = secrets.token_hex(8)
        with self.connect() as conn:
            conn.execute("DELETE FROM report_contexts WHERE expires_at < ?", (self._now(),))
            conn.execute(
                "INSERT INTO report_contexts VALUES (?, ?, ?, ?, ?)",
                (
                    key,
                    chat_id,
                    generation,
                    json.dumps(payload),
                    (datetime.now(UTC) + timedelta(days=30)).isoformat(),
                ),
            )
        return key

    def report_context(self, chat_id: int, key: str):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM report_contexts WHERE id = ? AND chat_id = ? AND expires_at >= ?",
                (key, chat_id, self._now()),
            ).fetchone()
            return dict(row) if row else None

    def report_failure(self, chat_id: int, notice: str, delay: int, reauth: bool = False) -> bool:
        with self.connect() as conn:
            user = conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
            if not user:
                return False
            conn.execute(
                "UPDATE users SET report_retry_at = ?, report_error_notice = ? WHERE chat_id = ?",
                ((datetime.now(UTC) + timedelta(seconds=delay)).isoformat(), notice, chat_id),
            )
            if reauth:
                conn.execute(
                    "UPDATE connections SET reauth_required = 1 WHERE chat_id = ?", (chat_id,)
                )
            return user["report_error_notice"] != notice

    def clear_report_failure(self, chat_id: int):
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET report_retry_at = NULL, report_error_notice = NULL WHERE chat_id = ?",
                (chat_id,),
            )

    def cleanup(self):
        with self.connect() as conn:
            conn.execute("DELETE FROM report_contexts WHERE expires_at < ?", (self._now(),))
            conn.execute("DELETE FROM oauth_states WHERE expires_at < ?", (self._now(),))
            conn.execute(
                "DELETE FROM events WHERE created_at < ?",
                ((datetime.now(UTC) - timedelta(days=90)).isoformat(),),
            )
