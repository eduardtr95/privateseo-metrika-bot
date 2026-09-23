from datetime import date, timedelta
from io import BytesIO
from unittest.mock import Mock

import pytest
from PIL import Image

from metrika_bot.analysis import BreakdownChange, Change, ReportData
from metrika_bot.bot import BotService
from metrika_bot.charts import render_chart
from metrika_bot.dashboard import (
    Dashboard,
    calendar_periods,
    collect_history,
    dashboard_text,
    source_selection,
)
from metrika_bot.db import Database


def report(mode="month", today=date(2026, 9, 23)):
    current, previous, title = calendar_periods(mode, today)
    return ReportData(
        "example.test",
        current,
        previous,
        Change(120, 100),
        Change(90, 80),
        Change(7, 5),
        ["Форма"],
        [BreakdownChange("Форма", 7, 5)],
        [BreakdownChange("Поиск", 80, 70), BreakdownChange("Соцсети", 40, 30)],
        [],
        source_ids={"Поиск": "organic", "Соцсети": "social"},
        valid_goal_ids=[1],
        dashboard=Dashboard(mode, title, ["organic"]),
    )


@pytest.mark.parametrize(
    "mode,today,expected",
    [
        (
            "day",
            date(2026, 9, 23),
            (date(2026, 9, 22), date(2026, 9, 22), date(2026, 9, 21), date(2026, 9, 21)),
        ),
        (
            "week",
            date(2026, 9, 23),
            (date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 14), date(2026, 9, 15)),
        ),
        (
            "week",
            date(2026, 9, 21),
            (date(2026, 9, 14), date(2026, 9, 20), date(2026, 9, 7), date(2026, 9, 13)),
        ),
        (
            "month",
            date(2026, 9, 23),
            (date(2026, 9, 1), date(2026, 9, 22), date(2026, 8, 1), date(2026, 8, 22)),
        ),
        (
            "month",
            date(2026, 9, 1),
            (date(2026, 8, 1), date(2026, 8, 31), date(2026, 7, 1), date(2026, 7, 31)),
        ),
        (
            "month",
            date(2026, 1, 1),
            (date(2025, 12, 1), date(2025, 12, 31), date(2025, 11, 1), date(2025, 11, 30)),
        ),
        (
            "month",
            date(2024, 3, 31),
            (date(2024, 3, 1), date(2024, 3, 30), date(2024, 2, 1), date(2024, 2, 29)),
        ),
    ],
)
def test_calendar_windows_and_boundaries(mode, today, expected):
    current, previous, _ = calendar_periods(mode, today)
    assert (current.start, current.end, previous.start, previous.end) == expected
    assert current.end < today


def test_calendar_text_is_readable_and_filters_sources_only():
    text = dashboard_text(report())
    assert "Сентябрь: 1–22 сентября" in text
    assert "Сравнение: 1–22 августа" in text
    assert "Соцсети" not in text
    assert "Поиск" in text and "Форма" in text
    assert "Всего визитов: <b>120</b>" in text
    assert "Итоги и цели — весь сайт" in text


def test_short_month_compares_daily_pace_instead_of_unequal_totals():
    data = report(today=date(2026, 3, 31))
    data.visits = Change(300, 280)
    assert "Всего визитов: <b>300</b> · +0% в день" in dashboard_text(data)
    assert "периоды 30 и 28 дн" in dashboard_text(data)


def test_preferences_persist_with_generation_check_and_delete(tmp_path):
    path = tmp_path / "bot.sqlite3"
    db = Database(path)
    db.upsert_user(123, "owner")
    db.save_tokens(123, "ciphertext", None, None)
    db.select_counter(123, 1, "example.test")
    generation = db.get_connection(123)["generation"]
    assert db.set_display(123, generation, sources=["organic", "direct"], chart=False, view="month")
    db = Database(path)
    row = db.get_connection(123)
    assert source_selection(row) == ["direct", "organic"]
    assert row["report_view"] == "month" and row["chart_enabled"] == 0
    db.select_counter(123, 2, "second.test")
    assert not db.set_display(123, generation, sources=["social"])
    assert source_selection(db.get_connection(123)) == ["direct", "organic"]
    db.delete_user(123)
    assert db.get_connection(123) is None


def test_history_uses_date_and_source_ids_and_unique_goal_filter():
    data = report()
    api = Mock()

    def response(*args, **kwargs):
        if args[5] == ["ym:s:date", "ym:s:trafficSource"]:
            assert "trafficSource=='organic'" in kwargs["filters"]
            return {
                "total_rows": 2,
                "data": [
                    {"dimensions": [{"id": "2026-09-21"}, {"id": "organic"}], "metrics": [10]},
                    {"dimensions": [{"id": "2026-09-22"}, {"id": "organic"}], "metrics": [None]},
                ],
            }
        assert "goal1IsReached=='yes'" in kwargs["filters"]
        return {"total_rows": 1, "data": [{"dimensions": [{"id": "2026-09-22"}], "metrics": [2]}]}

    api.report.side_effect = response
    collect_history(api, 123, 1, data)
    assert data.dashboard.series["organic"][-2:] == [10, None]
    assert data.dashboard.series["organic"][0] == 0
    assert data.dashboard.goal_series[-1] == 2
    assert len(data.dashboard.dates) == 53


def test_incomplete_history_is_never_drawn_as_zero():
    api = Mock()
    api.report.side_effect = [
        {
            "total_rows": 2,
            "data": [{"dimensions": [{"id": "2026-09-22"}, {"id": "organic"}], "metrics": [2]}],
        },
        {"total_rows": 2, "data": []},
    ]
    with pytest.raises(ValueError, match="Incomplete"):
        collect_history(api, 123, 1, report())


def test_chart_is_png_and_handles_empty_zero_and_missing_values():
    data = report()
    data.dashboard.dates = [date(2026, 9, 9) + timedelta(days=i) for i in range(14)]
    data.dashboard.series = {"organic": [None, 0, 1, 2, 0, 0, 4, 0, 0, 0, 0, 1, 2, 0]}
    data.dashboard.goal_series = [0] * 14
    png = render_chart(data)
    im = Image.open(BytesIO(png))
    assert im.format == "PNG" and im.width == 1000 and im.height < 900
    data.dashboard.chart_warning = "Incomplete"
    assert render_chart(data) is None


def test_failed_chart_switch_never_relabels_old_chart_with_new_period():
    service = object.__new__(BotService)
    service.telegram = Mock()
    data = report()
    data.dashboard.chart_warning = "График временно недоступен"
    service._send_dashboard(123, data, "snapshot", message_id=99, has_photo=True)
    service.telegram.edit_chart_caption.assert_not_called()
    service.telegram.send_message.assert_called_once()


def test_old_source_keyboard_cannot_change_new_counter(tmp_path):
    db = Database(tmp_path / "bot.sqlite3")
    db.upsert_user(123, "owner")
    db.save_tokens(123, "ciphertext", None, None)
    db.select_counter(123, 1, "first")
    gen = db.get_connection(123)["generation"]
    db.select_counter(123, 2, "second")
    service = object.__new__(BotService)
    service.db = db
    service.telegram = Mock()
    service._handle_callback(
        {
            "id": "cb",
            "message": {"message_id": 1, "chat": {"id": 123, "type": "private"}},
            "data": f"src:{gen}:none",
        }
    )
    assert db.get_connection(123)["visible_sources"] is None
    assert "устарели" in service.telegram.send_message.call_args.args[1]


def test_optional_chart_failure_does_not_stop_report(monkeypatch):
    service = object.__new__(BotService)
    service.telegram = Mock()

    def fail(_):
        raise ImportError("chart dependency unavailable")

    monkeypatch.setattr("metrika_bot.bot.render_chart", fail)
    service._send_dashboard(123, report(), "snapshot")
    assert "График временно недоступен" in service.telegram.send_message.call_args.args[1]
    assert "Всего визитов" in service.telegram.send_message.call_args.args[1]


def test_button_mode_skips_history_and_rendering(monkeypatch):
    from metrika_bot.dashboard import collect_dashboard

    builder = Mock()
    builder.collect.return_value = report()
    history = Mock(side_effect=AssertionError("History must be lazy"))
    renderer = Mock(side_effect=AssertionError("Rendering must be lazy"))
    monkeypatch.setattr("metrika_bot.dashboard.collect_history", history)
    monkeypatch.setattr("metrika_bot.bot.render_chart", renderer)
    data = collect_dashboard(builder, 123, {"chart_enabled": 0}, date(2026, 9, 23), "month")
    service = object.__new__(BotService)
    service.telegram = Mock()
    service._send_dashboard(123, data, "snapshot", message_id=99)
    args = service.telegram.edit_message_text.call_args.args
    assert args[:2] == (123, 99)
    assert "Форма" in args[2] and "Всего визитов" in args[2]
    assert any(b["callback_data"] == "chart:snapshot" for row in args[3] for b in row)
    history.assert_not_called()
    renderer.assert_not_called()
    service.telegram.send_chart.assert_not_called()


def test_requested_graph_keeps_original_snapshot_and_saved_mode(tmp_path):
    from types import SimpleNamespace

    db = Database(tmp_path / "bot.sqlite3")
    db.upsert_user(123, "owner")
    db.save_tokens(123, "ciphertext", None, None)
    db.select_counter(123, 1, "first")
    generation = db.get_connection(123)["generation"]
    db.set_goals(123, [1])
    db.set_display(123, generation, sources=["organic"], chart=False, view="month")
    original = dict(db.get_connection(123))
    snapshot = db.save_report_context(
        123,
        generation,
        {
            "connection": {
                k: original[k]
                for k in (
                    "counter_id",
                    "counter_name",
                    "goal_ids",
                    "visible_sources",
                    "chart_enabled",
                    "report_view",
                )
            },
            "today": "2026-09-23",
            "days": 7,
            "view": "month",
        },
    )
    db.set_goals(123, [2])
    db.set_display(123, generation, sources=["direct"])
    service = BotService(SimpleNamespace(report_timezone="Europe/Moscow"), db, Mock(), Mock())
    service.jobs.submit = lambda _, work: (work(), True)[1]
    service._run_report = Mock()
    message = {"message_id": 99, "chat": {"id": 123, "type": "private"}}
    try:
        service._handle_callback({"id": "cb", "message": message, "data": f"chart:{snapshot}"})
        args = service._run_report.call_args.args
        assert args[1]["goal_ids"] == "[1]"
        assert source_selection(args[1]) == ["organic"]
        assert args[1]["chart_enabled"] == 1
        assert args[2] == date(2026, 9, 23)
        assert args[7] == "month" and args[8] is None
        assert db.get_connection(123)["chart_enabled"] == 0
        service._handle_callback(
            {"id": "cb", "message": {**message, "photo": [{}]}, "data": f"v:{snapshot}:week"}
        )
        args = service._run_report.call_args.args
        assert args[1]["chart_enabled"] == 1 and args[8:10] == (99, True)
        assert db.get_connection(123)["chart_enabled"] == 0
        service._handle_callback({"id": "cb", "message": message, "data": f"v:{snapshot}:day"})
        assert service._run_report.call_args.args[1]["chart_enabled"] == 0
        service._run_report.reset_mock()
        db.select_counter(123, 2, "second")
        service._handle_callback({"id": "cb", "message": message, "data": f"chart:{snapshot}"})
        service._run_report.assert_not_called()
        assert "устарел" in service.telegram.send_message.call_args.args[1]
    finally:
        service.stop()
