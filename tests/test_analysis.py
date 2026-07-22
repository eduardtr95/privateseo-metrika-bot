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
        goal_details=[BreakdownChange("Заявка", 8, 12)],
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
    assert any("Поиск" in item for item in notes)
    assert any("Страница: service" in item for item in notes)


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
    assert insights(data) == [
        "Срочных действий нет: заметных провалов по источникам и страницам не найдено."
    ]


def test_traffic_growth_without_goal_growth_is_flagged():
    data = report(
        visits=Change(150, 100),
        users=Change(130, 90),
        goals=Change(10, 10),
        sources=[],
        pages=[],
    )
    assert any("визиты выросли, а бизнес-действия" in item for item in insights(data))


def test_html_is_escaped_and_message_fits_telegram():
    text = format_report(report(counter_name="<example>"))
    assert "&lt;example&gt;" in text
    assert len(text) <= 4096
    assert "Что делать" in text


def test_real_report_explains_hidden_search_loss_and_bad_goal():
    data = report(
        counter_name="private-seo.ru",
        visits=Change(107, 119),
        users=Change(88, 99),
        goals=Change(0, 0),
        goal_names=["Переход в YouTube"],
        goal_details=[BreakdownChange("Переход в YouTube", 0, 0)],
        sources=[
            BreakdownChange("Переходы из поисковых систем", 36, 56),
            BreakdownChange("Переходы по ссылкам на сайтах", 16, 6),
            BreakdownChange("Внутренние переходы", 7, 13),
            BreakdownChange("Прямые заходы", 42, 38),
        ],
        pages=[
            BreakdownChange("https://private-seo.ru/", 28, 36),
            BreakdownChange(
                "https://private-seo.ru/blog/seo-dlya-sportivnogo-magazina-sezonnye-tovary",
                0,
                7,
            ),
            BreakdownChange(
                "https://private-seo.ru/instrumenty/rasshireniye-dlya-seo-audita", 8, 2
            ),
            BreakdownChange("https://private-seo.ru/blog/chto-takoe-geo-prodvizhenie-dannye", 5, 0),
        ],
    )
    text = format_report(data)
    assert "🔴 Поиск: 36 ← 56 · −20 (−36%)" in text
    assert "🟢 Ссылки с сайтов: 16 ← 6 · +10 (+167%)" in text
    assert "Посадочные страницы: наибольшие потери" in text
    assert "Посадочные страницы: наибольший рост" in text
    assert "До 3 страниц в каждом блоке" in text
    assert "от 10 визитов независимо от процента" in text
    assert "до 500 самых посещаемых страниц" in text
    assert "Переход в YouTube" in text and "это не заявка" in text
    assert "Существенных изменений" not in text


def test_large_absolute_page_change_is_not_hidden_by_small_percentage():
    data = report(
        visits=Change(100_000, 100_000),
        pages=[BreakdownChange("https://example.ru/large", 950, 1_000)],
        sources=[],
    )
    assert "Страница: large" in format_report(data)
