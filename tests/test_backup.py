import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from metrika_bot.backup import create_backup


UTC = timezone.utc


def test_backup_is_consistent_private_and_removes_expired_files(tmp_path: Path):
    database = tmp_path / "source.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE example(value TEXT)")
        conn.execute("INSERT INTO example VALUES ('saved')")

    output = tmp_path / "backups"
    output.mkdir()
    expired = output / "bot-20260101T000000Z.sqlite3"
    expired.write_bytes(b"expired")
    old_time = (datetime(2026, 7, 1, tzinfo=UTC) - timedelta(days=30)).timestamp()
    os.utime(expired, (old_time, old_time))

    backup = create_backup(
        database,
        output,
        keep_days=14,
        now=datetime(2026, 7, 1, tzinfo=UTC),
    )

    assert backup.exists()
    assert backup.stat().st_mode & 0o777 == 0o600
    assert output.stat().st_mode & 0o777 == 0o700
    assert not expired.exists()
    with sqlite3.connect(backup) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT value FROM example").fetchone()[0] == "saved"
