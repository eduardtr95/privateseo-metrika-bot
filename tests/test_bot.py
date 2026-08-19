from pathlib import Path
from types import SimpleNamespace

from metrika_bot.analysis import BreakdownChange, Change, Period, ReportData
from metrika_bot.bot import BotService, counter_button_labels
from metrika_bot.db import Database
from datetime import date


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

    def send_chat_action(self, *args, **kwargs):
        del args, kwargs

    def answer_callback(self, *args, **kwargs):
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


def test_duplicate_counter_names_are_disambiguated_by_site_or_id():
    labels = counter_button_labels(
        [
            {"id": 1, "name": "Ягуар", "site": "jaguar-one.ru"},
            {"id": 2, "name": "Ягуар", "site": "jaguar-two.ru"},
            {"id": 3, "name": "Один счётчик", "site": "single.ru"},
            {"id": 4, "name": "Без сайта"},
            {"id": 5, "name": "Без сайта"},
        ]
    )

    assert labels == [
        "Ягуар · jaguar-one.ru · #1",
        "Ягуар · jaguar-two.ru · #2",
        "Один счётчик",
        "Без сайта · #4",
        "Без сайта · #5",
    ]


def test_full_report_callback_requests_detailed_view(tmp_path: Path):
    db = Database(tmp_path / "bot.sqlite3")
    service = object.__new__(BotService)
    service.db = db
    service.telegram = TelegramStub()
    calls = []
    service.send_report = lambda chat_id, detailed=False: calls.append((chat_id, detailed))

    service._handle_callback(
        {
            "id": "callback-1",
            "message": {"chat": {"id": 123, "type": "private"}},
            "from": {"username": "user"},
            "data": "week:full",
        }
    )

    assert calls == [(123, True)]


class ReportTelegramStub(TelegramStub):
    def __init__(self):
        self.rich_messages = []

    def send_rich_message(self, chat_id, text, buttons):
        self.rich_messages.append((chat_id, text, buttons))


def test_default_report_is_compact_and_links_to_details():
    service = object.__new__(BotService)
    service.telegram = ReportTelegramStub()
    data = ReportData(
        counter_name="example.ru",
        current_period=Period(date(2026, 8, 12), date(2026, 8, 18)),
        previous_period=Period(date(2026, 8, 5), date(2026, 8, 11)),
        visits=Change(120, 100),
        users=Change(100, 90),
        goals=Change(8, 6),
        goal_names=["Заявка"],
        goal_details=[BreakdownChange("Заявка", 8, 6)],
        sources=[BreakdownChange("Переходы из поисковых систем", 70, 50)],
        pages=[BreakdownChange("https://example.ru/service", 20, 30)],
    )

    service._send_formatted_report(123, data, with_buttons=True)

    _, text, buttons = service.telegram.rich_messages[0]
    assert "<table" not in text
    assert buttons[0][0] == {"text": "Показать детали", "callback_data": "week:full"}
