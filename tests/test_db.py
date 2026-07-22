import json
from pathlib import Path

from metrika_bot.db import Database


def test_oauth_state_is_one_time(tmp_path: Path):
    db = Database(tmp_path / "bot.sqlite3")
    db.upsert_user(123, "user")
    db.save_oauth_state("state", 123, "verifier")
    row = db.consume_oauth_state("state")
    assert row is not None
    assert row["chat_id"] == 123
    assert row["code_verifier"] == "verifier"
    assert db.consume_oauth_state("state") is None


def test_disconnect_removes_tokens_but_keeps_user(tmp_path: Path):
    db = Database(tmp_path / "bot.sqlite3")
    db.upsert_user(123, "user")
    db.save_tokens(123, "encrypted", None, None)
    db.select_counter(123, 55, "example")
    db.set_goals(123, [3, 1, 3])
    assert json.loads(db.get_connection(123)["goal_ids"]) == [1, 3]
    db.disconnect(123)
    assert db.get_connection(123) is None


def test_due_report_is_idempotent(tmp_path: Path):
    db = Database(tmp_path / "bot.sqlite3")
    db.upsert_user(123, "user")
    db.save_tokens(123, "encrypted", None, None)
    db.select_counter(123, 55, "example")
    assert [row["chat_id"] for row in db.due_users("2026-W30")] == [123]
    db.mark_report_sent(123, "2026-W30")
    assert db.due_users("2026-W30") == []
