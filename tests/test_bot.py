from pathlib import Path
from types import SimpleNamespace

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


class TelegramStub:
    def send_message(self, *args, **kwargs):
        del args, kwargs


class YandexStub:
    def authorization_url(self, chat_id):
        return f"https://example.test/{chat_id}"


def test_start_records_safe_first_touch_payload(tmp_path: Path):
    db = Database(tmp_path / "bot.sqlite3")
    service = object.__new__(BotService)
    service.db = db
    service.telegram = TelegramStub()
    service.yandex = YandexStub()
    service.config = SimpleNamespace(monitor_bot_url="https://example.test")

    service._handle_message(
        {
            "chat": {"id": 123, "type": "private"},
            "from": {"username": "user"},
            "text": "/start instagram_reels",
        }
    )
    service._handle_message(
        {
            "chat": {"id": 123, "type": "private"},
            "from": {"username": "user"},
            "text": "/start telegram_post",
        }
    )

    assert db.get_user(123)["first_start_payload"] == "instagram_reels"


def test_invalid_start_payload_is_recorded_as_direct(tmp_path: Path):
    db = Database(tmp_path / "bot.sqlite3")
    service = object.__new__(BotService)
    service.db = db
    service.telegram = TelegramStub()
    service.yandex = YandexStub()
    service.config = SimpleNamespace(monitor_bot_url="https://example.test")

    service._handle_message(
        {
            "chat": {"id": 123, "type": "private"},
            "from": {"username": "user"},
            "text": "/start not allowed?",
        }
    )

    assert db.get_user(123)["first_start_payload"] == "direct"
