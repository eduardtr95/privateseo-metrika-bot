import json
from datetime import date

from metrika_bot.analysis import (
    BreakdownChange,
    Change,
    Period,
    ReportData,
    ReportBuilder,
    completed_periods,
    completed_weeks,
    format_compact_report,
    format_compact_rich_report,
    format_report,
    format_rich_report,
    goal_relevance,
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


def test_daily_period_compares_yesterday_with_day_before():
    current, previous = completed_periods(1, date(2026, 7, 22))
    assert (current.start, current.end) == (date(2026, 7, 21), date(2026, 7, 21))
    assert (previous.start, previous.end) == (date(2026, 7, 20), date(2026, 7, 20))
    data = report(current_period=current, previous_period=previous)
    text = format_rich_report(data)
    assert "Итог дня" in text
    assert "21.07.2026 против предыдущего дня" in text


def test_report_points_to_largest_source_and_page_loss():
    notes = insights(report())
    assert any("Поиск" in item for item in notes)
    assert any("Страница: service" in item for item in notes)


def test_internal_transitions_are_not_presented_as_actionable_acquisition_loss():
    data = report(
        sources=[
            BreakdownChange("Внутренние переходы", 4, 24),
            BreakdownChange("Переходы из поисковых систем", 77, 52),
        ],
        pages=[],
    )
    text = format_report(data)
    assert "Внутренние переходы" not in text
    assert "Поиск: 77 ← 52" in text
    assert not any("Внутренние переходы" in note for note in insights(data))


def test_three_visit_source_change_is_visible_but_not_an_action():
    data = report(
        sources=[BreakdownChange("Переходы из социальных сетей", 5, 8)],
        pages=[],
    )
    assert "Соцсети: 5 ← 8" in format_report(data)
    assert not any("Соцсети" in note for note in insights(data))


def test_zero_to_zero_goal_is_not_labeled_as_new():
    text = format_report(report(goal_details=[BreakdownChange("Заявка", 0, 0)], goals=Change(0, 0)))
    assert "Заявка: 0 ← 0 · 0 (0%)" in text


def test_goal_recommendation_rejects_pre_conversion_actions():
    assert goal_relevance("Открытие формы заявки") == 0
    assert goal_relevance("Отправить на телефон") == 0
    assert goal_relevance("Успешная отправка заявки") == 2
    assert goal_relevance("Клик по телефону") == 1


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


def test_rich_report_uses_native_tables_and_links():
    text = format_rich_report(report())
    assert "<table bordered striped><caption>Итог недели</caption>" in text
    assert "<th>Метрика</th><th>Было → стало</th><th>Δ</th>" in text
    assert '<td align="center">120 → 80</td>' in text
    assert '<a href="https://example.ru/service">Страница: service</a>' in text
    assert "<ol><li>" in text
    assert len(text.encode()) <= 32768


def test_compact_report_has_no_table_and_only_two_highlights():
    data = report(
        sources=[
            BreakdownChange("Переходы из поисковых систем", 75, 40),
            BreakdownChange("Прямые заходы", 60, 45),
        ],
        pages=[
            BreakdownChange("https://example.ru/lost", 10, 30),
            BreakdownChange("https://example.ru/gained", 40, 10),
        ],
    )
    rich = format_compact_rich_report(data)
    fallback = format_compact_report(data)

    assert "<table" not in rich
    assert "Визиты:</b> 80" in rich
    assert "Целевые визиты:</b> 8" in rich
    assert "Поиск" in rich
    assert "Страница: lost" in rich
    assert "Прямые заходы" not in rich
    assert "Страница: gained" not in rich
    assert len(fallback.splitlines()) <= 15


def test_compact_report_warns_when_business_goals_are_not_selected():
    data = report(
        goals=None,
        goal_names=["Переход в YouTube"],
        goal_details=[BreakdownChange("Переход в YouTube", 10, 5)],
    )

    text = format_compact_report(data)

    assert "вспомогательные цели" in text
    assert "Целевые визиты:" not in text


class FakeYandex:
    def __init__(self):
        self.calls = []

    def goals(self, chat_id, counter_id):
        del chat_id, counter_id
        return [{"id": goal_id, "name": f"Заявка {goal_id}"} for goal_id in range(1, 13)] + [
            {"id": 13, "name": "Переход в YouTube"},
            {"id": 14, "name": "Запуск аудита"},
            {"id": 15, "name": "Переход в ТГ-канал"},
        ]

    def report(
        self,
        chat_id,
        counter_id,
        date1,
        date2,
        metrics,
        dimensions=None,
        limit=100,
        filters=None,
    ):
        del chat_id, counter_id, date2, limit
        self.calls.append({"metrics": metrics, "dimensions": dimensions, "filters": filters})
        current = date1 == "2026-07-15"
        if filters:
            return {"totals": [6 if current else 5]}
        if dimensions:
            return {"totals": [0], "data": []}
        if metrics == ["ym:s:visits", "ym:s:users"]:
            return {"totals": [100, 80] if current else [90, 70]}
        return {"totals": [1 if current else 0] * len(metrics)}


def test_report_uses_unique_business_goal_visits_and_batches_goal_metrics():
    yandex = FakeYandex()
    data = ReportBuilder(yandex).collect(
        123,
        {
            "counter_id": 1,
            "counter_name": "example.ru",
            "goal_ids": json.dumps(list(range(1, 16))),
        },
        today=date(2026, 7, 22),
    )

    assert data.goals == Change(6, 5)
    assert len(data.goal_details) == 15
    goal_metric_calls = [
        call for call in yandex.calls if call["metrics"] and call["metrics"][0].endswith("reaches")
    ]
    assert [len(call["metrics"]) for call in goal_metric_calls] == [10, 10, 5, 5]
    assert all(len(call["metrics"]) <= 10 for call in yandex.calls)
    goal_filters = [call["filters"] for call in yandex.calls if call["filters"]]
    assert len(goal_filters) == 2
    assert "goal12IsReached" in goal_filters[0]
    assert "goal13IsReached" not in goal_filters[0]


def test_report_labels_unique_visits_and_excludes_auxiliary_goals_from_total():
    text = format_report(
        report(
            goals=Change(6, 5),
            goal_names=["Заявка", "Переход в YouTube"],
            goal_details=[
                BreakdownChange("Заявка", 7, 6),
                BreakdownChange("Переход в YouTube", 20, 10),
            ],
        )
    )
    assert "Целевые визиты без дублей: 6 ← 5 · +20%" in text
    assert "Один визит может достичь нескольких целей" in text
    assert "Не входят в итог как заявки: «Переход в YouTube»" in text
