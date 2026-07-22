from datetime import date

from metrika_bot.analysis import (
    BreakdownChange,
    Change,
    Period,
    ReportData,
    completed_weeks,
    format_report,
    insights,
)


def report(**overrides):
    data = ReportData(
        counter_name="example.ru",
        current_period=Period(date(2026, 7, 15), date(2026, 7, 21)),
        previous_period=Period(date(2026, 7, 8), date(2026, 7, 14)),
        visits=Change(80, 120),
        users=Change(70, 100),
        goals=Change(8, 12),
        goal_names=["Заявка"],
        sources=[BreakdownChange("Переходы из поисковых систем", 40, 75)],
        pages=[BreakdownChange("https://example.ru/service", 20, 50)],
    )
    for key, value in overrides.items():
        setattr(data, key, value)
    return data


def test_completed_weeks_uses_last_completed_day():
    current, previous = completed_weeks(date(2026, 7, 22))
    assert (current.start, current.end) == (date(2026, 7, 15), date(2026, 7, 21))
    assert (previous.start, previous.end) == (date(2026, 7, 8), date(2026, 7, 14))


def test_report_points_to_largest_source_and_page_loss():
    notes = insights(report())
    assert any("поисковых систем" in item for item in notes)
    assert any("example.ru/service" in item for item in notes)


def test_goals_drop_with_stable_traffic_checks_forms():
    data = report(
        visits=Change(101, 100),
        users=Change(90, 89),
        goals=Change(4, 10),
        sources=[],
        pages=[],
    )
    assert any("формы" in item for item in insights(data))


def test_small_numbers_do_not_create_false_alarm():
    data = report(
        visits=Change(2, 4),
        users=Change(2, 3),
        goals=Change(0, 1),
        sources=[],
        pages=[],
    )
    assert insights(data) == ["🟢 Существенных изменений, требующих реакции, не найдено."]


def test_traffic_growth_without_goal_growth_is_flagged():
    data = report(
        visits=Change(150, 100),
        users=Change(130, 90),
        goals=Change(10, 10),
        sources=[],
        pages=[],
    )
    assert any("Трафик вырос, а цели" in item for item in insights(data))


def test_html_is_escaped_and_message_fits_telegram():
    text = format_report(report(counter_name="<example>"))
    assert "&lt;example&gt;" in text
    assert len(text) <= 4096
    assert "Что требует внимания" in text
