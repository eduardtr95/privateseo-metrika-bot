from pathlib import Path

from metrika_bot.bot import BotService
from metrika_bot.db import Database


def test_blocking_bot_deletes_all_private_user_data(tmp_path: Path):
    db = Database(tmp_path / "bot.sqlite3")
    db.upsert_user(123, "user")
    db.save_tokens(123, "encrypted", None, None)
    db.event(123, "report_manual")
    service = object.__new__(BotService)
    service.db = db

    service._handle_my_chat_member(
        {
            "chat": {"id": 123, "type": "private"},
            "new_chat_member": {"status": "kicked"},
        }
    )

    assert db.get_user(123) is None
    assert db.get_connection(123) is None
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM events WHERE chat_id = 123").fetchone()[0] == 0
