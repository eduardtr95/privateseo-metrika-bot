from __future__ import annotations

import html
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from .formatting import fit_html

from .yandex import YandexClient


@dataclass(frozen=True)
class Period:
    start: date
    end: date

    @property
    def api_start(self) -> str:
        return self.start.isoformat()

    @property
    def api_end(self) -> str:
        return self.end.isoformat()


@dataclass(frozen=True)
class Change:
    current: float
    previous: float

    @property
    def absolute(self) -> float:
        return self.current - self.previous

    @property
    def percent(self) -> float | None:
        if self.previous == 0:
            return None
        return (self.current - self.previous) / self.previous * 100


@dataclass
class BreakdownChange:
    name: str
    current: float
    previous: float

    @property
    def delta(self) -> float:
        return self.current - self.previous

    @property
    def percent(self) -> float | None:
        if self.previous == 0:
            return None
        return self.delta / self.previous * 100


@dataclass
class ReportData:
    counter_name: str
    current_period: Period
    previous_period: Period
    visits: Change
    users: Change
    goals: Change | None
    goal_names: list[str]
    goal_details: list[BreakdownChange]
    sources: list[BreakdownChange]
    pages: list[BreakdownChange]
    sampled: bool = False
    pages_partial: bool = False
    missing_goals: list[int] = field(default_factory=list)
    timezone_name: str = "Europe/Moscow"
    data_delayed: bool = False
    source_ids: dict[str, str] = field(default_factory=dict)
    valid_goal_ids: list[int] = field(default_factory=list)
    dashboard: Any = None
    pages_loaded: bool = True


def completed_weeks(today: date | None = None) -> tuple[Period, Period]:
    return completed_periods(7, today)


def completed_periods(days: int, today: date | None = None) -> tuple[Period, Period]:
    if days < 1:
        raise ValueError("days must be positive")
    today = today or date.today()
    current_end = today - timedelta(days=1)
    current = Period(current_end - timedelta(days=days - 1), current_end)
    previous_end = current.start - timedelta(days=1)
    previous = Period(previous_end - timedelta(days=days - 1), previous_end)
    return current, previous


def _totals(payload: dict[str, Any]) -> list[float]:
    totals = payload.get("totals") or []
    return [float(value or 0) for value in totals]


def _breakdown(payload: dict[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in payload.get("data", []):
        dimensions = row.get("dimensions") or []
        if not dimensions:
            continue
        dim = dimensions[0]
        name = str(dim.get("name") or dim.get("id") or "Не определено")
        result[name] = result.get(name, 0) + float((row.get("metrics") or [0])[0] or 0)
    return result


def compare_breakdowns(
    current: dict[str, float],
    previous: dict[str, float],
    *,
    current_complete: bool = True,
    previous_complete: bool = True,
) -> list[BreakdownChange]:
    return [
        BreakdownChange(name, current.get(name, 0), previous.get(name, 0))
        for name in sorted(set(current) | set(previous))
        if (name in current or current_complete) and (name in previous or previous_complete)
    ]


class ReportBuilder:
    def __init__(self, yandex: YandexClient, timezone_name: str = "Europe/Moscow"):
        self.yandex = yandex
        self.timezone_name = timezone_name

    def _pages(self, chat_id: int, counter_id: int, period: Period) -> tuple[dict, bool]:
        payload = self.yandex.report(
            chat_id,
            counter_id,
            period.api_start,
            period.api_end,
            ["ym:s:visits"],
            ["ym:s:startURL"],
            limit=500,
        )
        rows = list(payload.get("data", []))
        total = int(payload.get("total_rows", len(rows)))
        while len(rows) < total and len(rows) < 5000:
            batch = self.yandex.report(
                chat_id,
                counter_id,
                period.api_start,
                period.api_end,
                ["ym:s:visits"],
                ["ym:s:startURL"],
                limit=500,
                offset=len(rows) + 1,
            )
            chunk = batch.get("data", [])
            payload["sampled"] = bool(payload.get("sampled") or batch.get("sampled"))
            if not chunk:
                break
            rows.extend(chunk)
        payload["data"] = rows
        return payload, len(rows) >= total

    def collect(
        self,
        chat_id: int,
        connection: Any,
        today: date | None = None,
        days: int = 7,
        periods: tuple[Period, Period] | None = None,
        include_pages: bool = True,
    ) -> ReportData:
        counter_id = int(connection["counter_id"])
        goal_ids = [int(value) for value in json.loads(connection["goal_ids"] or "[]")]
        goals = self.yandex.goals(chat_id, counter_id)
        goal_map = {int(goal["id"]): str(goal.get("name") or goal["id"]) for goal in goals}
        selected = [goal_id for goal_id in goal_ids if goal_id in goal_map][:15]
        today = today or datetime.now(ZoneInfo(self.timezone_name)).date()
        current, previous = periods or completed_periods(days, today)

        cur_total = self.yandex.report(
            chat_id,
            counter_id,
            current.api_start,
            current.api_end,
            ["ym:s:visits", "ym:s:users"],
        )
        prev_total = self.yandex.report(
            chat_id,
            counter_id,
            previous.api_start,
            previous.api_end,
            ["ym:s:visits", "ym:s:users"],
        )
        cur_values, prev_values = _totals(cur_total), _totals(prev_total)

        goal_payloads: list[dict[str, Any]] = []
        cur_goal_values: list[float] = []
        prev_goal_values: list[float] = []
        # Reporting API accepts at most ten metrics in one request. Goal details
        # are deliberately batched so the advertised 15-goal selection works.
        for offset in range(0, len(selected), 10):
            goal_batch = selected[offset : offset + 10]
            goal_metrics = [f"ym:s:goal{goal_id}reaches" for goal_id in goal_batch]
            cur_payload = self.yandex.report(
                chat_id,
                counter_id,
                current.api_start,
                current.api_end,
                goal_metrics,
            )
            prev_payload = self.yandex.report(
                chat_id,
                counter_id,
                previous.api_start,
                previous.api_end,
                goal_metrics,
            )
            goal_payloads.extend((cur_payload, prev_payload))
            cur_goal_values.extend(_totals(cur_payload))
            prev_goal_values.extend(_totals(prev_payload))

        selected_goal_ids = selected
        goal_visit_payloads: list[dict[str, Any]] = []
        cur_goals: float | None = None
        prev_goals: float | None = None
        if selected_goal_ids:
            goal_filter = " OR ".join(
                f"ym:s:goal{goal_id}IsReached=='yes'" for goal_id in selected_goal_ids
            )
            cur_goal_visits = self.yandex.report(
                chat_id,
                counter_id,
                current.api_start,
                current.api_end,
                ["ym:s:visits"],
                filters=goal_filter,
            )
            prev_goal_visits = self.yandex.report(
                chat_id,
                counter_id,
                previous.api_start,
                previous.api_end,
                ["ym:s:visits"],
                filters=goal_filter,
            )
            goal_visit_payloads.extend((cur_goal_visits, prev_goal_visits))
            cur_goals = (_totals(cur_goal_visits) or [0])[0]
            prev_goals = (_totals(prev_goal_visits) or [0])[0]

        goal_details = [
            BreakdownChange(goal_map[goal_id], cur_goal_values[index], prev_goal_values[index])
            for index, goal_id in enumerate(selected)
        ]

        source_dimension = ["ym:s:trafficSource"]
        cur_sources = self.yandex.report(
            chat_id,
            counter_id,
            current.api_start,
            current.api_end,
            ["ym:s:visits"],
            source_dimension,
        )
        prev_sources = self.yandex.report(
            chat_id,
            counter_id,
            previous.api_start,
            previous.api_end,
            ["ym:s:visits"],
            source_dimension,
        )
        cur_pages, cur_complete = (
            self._pages(chat_id, counter_id, current) if include_pages else ({}, True)
        )
        prev_pages, prev_complete = (
            self._pages(chat_id, counter_id, previous) if include_pages else ({}, True)
        )
        sampled = any(
            payload.get("sampled") is True
            for payload in (
                cur_total,
                prev_total,
                cur_sources,
                prev_sources,
                cur_pages,
                prev_pages,
                *goal_payloads,
                *goal_visit_payloads,
            )
        )
        return ReportData(
            counter_name=str(connection["counter_name"] or counter_id),
            current_period=current,
            previous_period=previous,
            visits=Change(cur_values[0], prev_values[0]),
            users=Change(cur_values[1], prev_values[1]),
            goals=(
                Change(cur_goals, prev_goals)
                if cur_goals is not None and prev_goals is not None
                else None
            ),
            goal_names=[goal_map[goal_id] for goal_id in selected],
            goal_details=goal_details,
            sources=compare_breakdowns(_breakdown(cur_sources), _breakdown(prev_sources)),
            pages=compare_breakdowns(
                _breakdown(cur_pages),
                _breakdown(prev_pages),
                current_complete=cur_complete,
                previous_complete=prev_complete,
            ),
            pages_partial=not (cur_complete and prev_complete),
            missing_goals=[value for value in goal_ids if value not in goal_map],
            timezone_name=self.timezone_name,
            data_delayed=any((p.get("data_lag") or 0) > 3600 for p in (cur_total, prev_total)),
            sampled=sampled,
            source_ids={
                str(row["dimensions"][0].get("name") or row["dimensions"][0].get("id")): str(
                    row["dimensions"][0].get("id")
                )
                for payload in (cur_sources, prev_sources)
                for row in payload.get("data", [])
                if row.get("dimensions")
            },
            valid_goal_ids=selected,
            pages_loaded=include_pages,
        )

    def add_pages(self, chat_id, counter_id, data):
        current, cur_complete = self._pages(chat_id, counter_id, data.current_period)
        previous, prev_complete = self._pages(chat_id, counter_id, data.previous_period)
        data.pages = compare_breakdowns(
            _breakdown(current),
            _breakdown(previous),
            current_complete=cur_complete,
            previous_complete=prev_complete,
        )
        data.pages_partial = not (cur_complete and prev_complete)
        data.sampled = data.sampled or bool(current.get("sampled") or previous.get("sampled"))
        data.pages_loaded = True


def _number(value: float) -> str:
    return f"{round(value):,}".replace(",", " ")


def _change(change: Change) -> str:
    if change.percent is None:
        suffix = "новые данные" if change.current else "без данных"
    else:
        suffix = f"{change.percent:+.0f}%"
    return f"{_number(change.current)} ← {_number(change.previous)} · {suffix}"


def _short_page(value: str, limit: int = 58) -> str:
    value = value.replace("https://", "").replace("http://", "")
    return value if len(value) <= limit else value[: limit - 1] + "…"


SOURCE_NAMES = {
    "Переходы из поисковых систем": "Поиск",
    "Переходы по ссылкам на сайтах": "Ссылки с сайтов",
    "Прямые заходы": "Прямые заходы",
    "Внутренние переходы": "Внутренние переходы",
    "Переходы из рекомендательных систем": "Рекомендации",
    "Переходы по рекламе": "Реклама",
    "Переходы из социальных сетей": "Соцсети",
}

SERVICE_SOURCES = {"Внутренние переходы"}


def source_name(value: str) -> str:
    return SOURCE_NAMES.get(value, value)


def goal_relevance(name: str) -> int:
    """2 = primary business goal, 1 = contact intent, 0 = auxiliary goal."""
    lowered = name.casefold()
    auxiliary = (
        "youtube",
        "ютуб",
        "канал",
        "открытие формы",
        "открыть форму",
        "отправить на телефон",
    )
    if any(term in lowered for term in auxiliary):
        return 0
    primary = ("заяв", "заказ", "покуп", "оплат", "лид", "диалог", "отправ")
    contact = ("телефон", "звон", "email", "e-mail", "мессенджер", "whatsapp", "чат")
    if any(term in lowered for term in primary):
        return 2
    if any(term in lowered for term in contact):
        return 1
    return 0


def _signed(value: float) -> str:
    if value > 0:
        return f"+{_number(value)}"
    if value < 0:
        return f"−{_number(abs(value))}"
    return "0"


def _signed_percent(value: float | None) -> str:
    if value is None:
        return "новое"
    sign = "+" if value > 0 else "−" if value < 0 else ""
    return f"{sign}{abs(value):.0f}%"


def _mover_line(item: BreakdownChange, label: str, link: str | None = None) -> str:
    marker = "🟢" if item.delta > 0 else "🔴" if item.delta < 0 else "⚪️"
    safe_label = html.escape(label)
    if link:
        safe_label = f'<a href="{html.escape(link, quote=True)}">{safe_label}</a>'
    return (
        f"{marker} {safe_label}: {_number(item.current)} ← {_number(item.previous)}"
        f" · {_signed(item.delta)} ({_breakdown_percent(item)})"
    )


def _page_label(value: str, limit: int = 48) -> str:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    path = parsed.path.rstrip("/")
    if not path:
        return "Главная"
    if path == "/blog":
        return "Блог"
    slug = path.rsplit("/", 1)[-1].replace("-", " ")
    prefix = "Статья: " if path.startswith("/blog/") else "Страница: "
    label = prefix + slug
    return label if len(label) <= limit else label[: limit - 1] + "…"


def _important(item: BreakdownChange, min_previous: float = 5) -> bool:
    return bool(
        abs(item.delta) >= 3
        and (item.previous >= min_previous or item.current >= min_previous)
        and (item.percent is None or abs(item.percent) >= 20 or abs(item.delta) >= 10)
    )


def _important_source(item: BreakdownChange) -> bool:
    return bool(
        abs(item.delta) >= 10
        and (item.previous >= 20 or item.current >= 20)
        and (item.percent is None or abs(item.percent) >= 20 or abs(item.delta) >= 25)
    )


def _meaningful(change: Change, min_previous: float, percent: float, absolute: float) -> bool:
    return bool(
        change.percent is not None
        and change.previous >= min_previous
        and abs(change.percent) >= percent
        and abs(change.absolute) >= absolute
    )


def insights(data: ReportData) -> list[str]:
    from .dashboard import visible_sources

    notes: list[str] = []
    source_losses = sorted(
        (
            item
            for item in visible_sources(data)
            if item.name not in SERVICE_SOURCES and item.delta < 0 and _important_source(item)
        ),
        key=lambda item: item.delta,
    )
    page_losses = sorted(
        (item for item in data.pages if item.delta < 0 and _important(item)),
        key=lambda item: item.delta,
    )

    if source_losses:
        lead = source_losses[0]
        notes.append(
            f"Проверить «{source_name(lead.name)}»: визиты снизились "
            f"с {_number(lead.previous)} до {_number(lead.current)} ({_signed_percent(lead.percent)})."
        )
    if page_losses:
        lead = page_losses[0]
        notes.append(
            f"Разобрать страницу «{_page_label(lead.name)}»: она потеряла "
            f"{_number(abs(lead.delta))} визитов."
        )
    if data.goals and _meaningful(data.goals, 5, 25, 2) and data.goals.absolute < 0:
        notes.append("Проверить формы и контакты: выбранных бизнес-действий стало заметно меньше.")
    elif (
        data.goals
        and data.visits.percent is not None
        and data.goals.percent is not None
        and data.visits.percent >= 20
        and data.goals.previous >= 5
        and data.goals.percent <= 0
    ):
        notes.append("Проверить качество нового трафика: визиты выросли, а бизнес-действия — нет.")

    if not notes:
        notes.append(
            "Срочных действий нет: заметных провалов по источникам и страницам не найдено."
        )
    if data.pages_partial and len(notes) == 1 and notes[0].startswith("Срочных действий нет"):
        notes = [
            "По доступным данным заметных провалов не найдено; часть страниц не удалось сравнить."
        ]
    notes.sort(
        key=lambda note: 0 if note.startswith(("Проверить формы", "Проверить качество")) else 1
    )
    return notes[:3]


def _summary(data: ReportData) -> list[str]:
    visits = data.visits
    direction = (
        "больше" if visits.absolute > 0 else "меньше" if visits.absolute < 0 else "столько же"
    )
    if visits.absolute:
        first = (
            f"Визитов стало на <b>{_number(abs(visits.absolute))} {direction}</b>: "
            f"{_number(visits.current)} ← {_number(visits.previous)} "
            f"({_signed_percent(visits.percent)})."
        )
    else:
        first = f"Визитов столько же: <b>{_number(visits.current)}</b>."

    return [first]


def _period_word(data: ReportData) -> tuple[str, str, str]:
    if data.dashboard:
        from .dashboard import human_period

        label = {"day": "день", "week": "неделю", "month": "месяц"}[data.dashboard.mode]
        return (
            label,
            data.dashboard.title,
            f"— сравнение: {human_period(data.previous_period, True)}",
        )
    days = (data.current_period.end - data.current_period.start).days + 1
    if days == 1:
        return "день", "Итог дня", "против предыдущего дня"
    return "неделю", "Итог недели", "против предыдущих 7 дней"


def _period_text(period: Period) -> str:
    if period.start == period.end:
        return period.start.strftime("%d.%m.%Y")
    return f"{period.start.strftime('%d.%m')}–{period.end.strftime('%d.%m.%Y')}"


def _report_movers(
    data: ReportData,
) -> tuple[list[BreakdownChange], list[BreakdownChange], list[BreakdownChange]]:
    from .dashboard import visible_sources

    sources = sorted(visible_sources(data), key=lambda item: abs(item.delta), reverse=True)
    sources = [
        item for item in sources if item.name not in SERVICE_SOURCES and abs(item.delta) >= 3
    ][:4]
    losses = sorted(
        (item for item in data.pages if item.delta < 0 and _important(item)),
        key=lambda item: item.delta,
    )[:3]
    gains = sorted(
        (item for item in data.pages if item.delta > 0 and _important(item)),
        key=lambda item: item.delta,
        reverse=True,
    )[:3]
    return sources, losses, gains


def _rich_delta(item: BreakdownChange) -> str:
    marker = "🟢" if item.delta > 0 else "🔴" if item.delta < 0 else "⚪️"
    return f"{marker} {_signed(item.delta)} · {_breakdown_percent(item)}"


def _breakdown_percent(item: BreakdownChange) -> str:
    if item.current == 0 and item.previous == 0:
        return "0%"
    return _signed_percent(item.percent)


def _rich_change(change: Change) -> str:
    marker = "🟢" if change.absolute > 0 else "🔴" if change.absolute < 0 else "⚪️"
    return f"{marker} {_signed(change.absolute)} · {_signed_percent(change.percent)}"


def _rich_table(
    caption: str,
    rows: list[tuple[str, float, float, str]],
    label_header: str,
) -> str:
    rendered = [
        f"<table bordered striped><caption>{html.escape(caption)}</caption>",
        f"<tr><th>{html.escape(label_header)}</th><th>Было → стало</th><th>Δ</th></tr>",
    ]
    for label, previous, current, change in rows:
        rendered.append(
            "<tr>"
            f"<td>{label}</td>"
            f'<td align="center">{_number(previous)} → {_number(current)}</td>'
            f'<td align="center">{html.escape(change)}</td>'
            "</tr>"
        )
    rendered.append("</table>")
    return "".join(rendered)


def _compact_value(change: Change, small_base: int = 20) -> str:
    """A single comparison, without dramatic percentages on tiny baselines."""
    value = f"<b>{_number(change.current)}</b>"
    if not change.absolute:
        delta = "без изменений"
    elif change.previous < small_base:
        delta = _signed(change.absolute)
    else:
        delta = _signed_percent(change.percent)
    return f"{value} · {delta}"


def format_compact_rich_report(data: ReportData) -> str:
    """Equivalent content for callers explicitly requesting rich HTML."""
    return "<p>" + format_compact_report(data).replace("\n", "<br>") + "</p>"


def format_compact_report(data: ReportData) -> str:
    """One-screen overview: totals and all active sources, without repeated advice."""
    zone = "МСК" if data.timezone_name == "Europe/Moscow" else data.timezone_name

    def dates(period: Period) -> str:
        # Keep both exact windows visible without repeating the year twice.
        year = data.current_period.end.year != data.previous_period.start.year
        end = period.end.strftime("%d.%m.%Y" if year else "%d.%m")
        if period.start == period.end:
            return end
        start = period.start.strftime("%d" if period.start.month == period.end.month else "%d.%m")
        return f"{start}–{end}"

    lines = [
        f"<b>{html.escape(data.counter_name)}</b>",
        f"{dates(data.current_period)} · к {dates(data.previous_period)} · {html.escape(zone)}",
        "",
        f"Визиты: {_compact_value(data.visits)}",
        f"Посетители: {_compact_value(data.users)}",
    ]
    if data.goals is not None and data.goal_names:
        lines.append(f"Целевые визиты: {_compact_value(data.goals, small_base=5)}")
    elif data.goal_names:
        lines.append("⚠️ Не удалось получить данные выбранных целей. Проверьте /goals.")
    elif not data.missing_goals:
        lines.append("Цели не выбраны → /goals")

    sources = sorted(data.sources, key=lambda item: (-item.current, -item.previous, item.name))
    sources = [item for item in sources if item.current or item.previous]
    if sources:
        lines.extend(["", "<b>Источники · визиты</b>"])
        for item in sources:
            label = source_name(item.name)
            lines.append(
                f"{html.escape(label)}: {_compact_value(Change(item.current, item.previous))}"
            )
    else:
        lines.extend(["", "По источникам визитов нет."])

    warnings = []
    if data.sampled:
        warnings.append("⚠️ Выборочные данные: изменения приблизительны.")
    if data.missing_goals:
        warnings.append("⚠️ Некоторые цели недоступны → /goals")
    if data.data_delayed:
        warnings.append("⚠️ Метрика ещё обновляет данные.")
    lines.extend(["", *warnings, "<i>Без распознанных роботов. Подробности — по кнопке.</i>"])
    return fit_html("\n".join(lines))


def format_rich_report(data: ReportData) -> str:
    """Telegram Bot API 10.2 native Rich Message with real tables."""
    period = data.current_period
    _, total_caption, comparison = _period_word(data)
    sources, page_losses, page_gains = _report_movers(data)
    blocks = [
        f"<h2>{html.escape(data.counter_name)}</h2>",
        (f"<p>{_period_text(period)} {comparison}</p>"),
        _rich_table(
            total_caption,
            [
                (
                    "Визиты",
                    data.visits.previous,
                    data.visits.current,
                    _rich_change(data.visits),
                ),
                (
                    "Посетители",
                    data.users.previous,
                    data.users.current,
                    _rich_change(data.users),
                ),
            ],
            "Метрика",
        ),
    ]

    if sources:
        blocks.append(
            _rich_table(
                "Главные изменения по источникам",
                [
                    (
                        html.escape(source_name(item.name)),
                        item.previous,
                        item.current,
                        _rich_delta(item),
                    )
                    for item in sources
                ],
                "Источник",
            )
        )

    def page_rows(items: list[BreakdownChange]) -> list[tuple[str, float, float, str]]:
        rows = []
        for item in items:
            label = html.escape(_page_label(item.name))
            label = f'<a href="{html.escape(item.name, quote=True)}">{label}</a>'
            rows.append((label, item.previous, item.current, _rich_delta(item)))
        return rows

    if page_losses:
        blocks.append(
            _rich_table("Посадочные страницы · потери", page_rows(page_losses), "Страница")
        )
    if page_gains:
        blocks.append(_rich_table("Посадочные страницы · рост", page_rows(page_gains), "Страница"))
    if page_losses or page_gains:
        blocks.append(
            "<footer>Показано до 3 страниц: ≥3 визитов и ≥20%, либо ≥10 визитов. "
            "Анализируются до 5 000 страниц каждого периода; пропуски неполной выгрузки не считаются нулями.</footer>"
        )

    selected_goals = data.goal_names
    if data.goals and selected_goals:
        selected_details = data.goal_details
        goal_rows = [
            (html.escape(item.name), item.previous, item.current, _rich_delta(item))
            for item in selected_details[:15]
        ]
        blocks.append(
            _rich_table(
                "Целевые визиты",
                [
                    (
                        "Хотя бы одна выбранная цель",
                        data.goals.previous,
                        data.goals.current,
                        _rich_change(data.goals),
                    )
                ],
                "Итог без дублей",
            )
        )
        if goal_rows:
            blocks.append(_rich_table("Достижения по целям", goal_rows, "Цель"))
            blocks.append(
                "<footer>Один целевой визит может включать несколько достижений; "
                "в итоговой строке такой визит считается один раз.</footer>"
            )
    elif data.goal_names:
        blocks.append(
            "<blockquote>⚠️ Не удалось получить данные выбранных целей. Проверьте /goals.</blockquote>"
        )
    else:
        blocks.append(
            "<blockquote>⚠️ Бизнес-цели не выбраны.<br>"
            "Настройте заявки, звонки, покупки или чат.</blockquote>"
        )

    actions = "".join(f"<li>{html.escape(note)}</li>" for note in insights(data))
    blocks.extend(["<h3>Что делать</h3>", f"<ol>{actions}</ol>"])
    if data.sampled:
        blocks.append(
            "<footer>Метрика применила семплирование: небольшие изменения могут быть неточными.</footer>"
        )
    blocks.extend(f"<footer>{html.escape(note)}</footer>" for note in report_notes(data))
    return "".join(blocks)


def format_report(data: ReportData, monitor_bot_url: str | None = None) -> str:
    period = data.current_period
    period_word, _, comparison = _period_word(data)
    lines = [
        f"<b>{html.escape(data.counter_name)}: что изменилось за {period_word}</b>",
        f"{_period_text(period)} {comparison}",
        "",
        "<b>Итог</b>",
    ]
    lines.extend(_summary(data))
    lines.append(f"Посетители: {_change(data.users)}")

    source_movers, page_losses, page_gains = _report_movers(data)
    if source_movers:
        lines.extend(["", "<b>Главные изменения по источникам</b>"])
        lines.extend(_mover_line(item, source_name(item.name)) for item in source_movers)

    if page_losses:
        lines.extend(["", "<b>Посадочные страницы: наибольшие потери</b>"])
        lines.extend(_mover_line(item, _page_label(item.name), item.name) for item in page_losses)
    if page_gains:
        lines.extend(["", "<b>Посадочные страницы: наибольший рост</b>"])
        lines.extend(_mover_line(item, _page_label(item.name), item.name) for item in page_gains)
    if page_losses or page_gains:
        lines.append(
            "<i>До 3 страниц в каждом блоке: изменение от 3 визитов и от 20%, "
            "либо от 10 визитов независимо от процента. "
            "Сравниваются до 5 000 страниц каждого периода; пропуски неполной выгрузки не считаются нулями.</i>"
        )

    lines.extend(["", "<b>Выбранные цели</b>"])
    selected_goals = data.goal_names
    if data.goals and selected_goals:
        lines.append(f"Целевые визиты без дублей: {_change(data.goals)}")
        selected_details = data.goal_details
        for item in selected_details[:15]:
            lines.append(_mover_line(item, item.name))
        lines.append(
            "<i>Один визит может достичь нескольких целей; в итоговой строке он считается один раз.</i>"
        )
    elif data.goal_names:
        lines.append("⚠️ Не удалось получить данные выбранных целей. Проверьте /goals.")
    else:
        lines.append("⚠️ Цели не выбраны. Настройте заявки, звонки, покупки или чат.")

    lines.extend(["", "<b>Что делать</b>"])
    lines.extend(
        f"{index}. {html.escape(note)}" for index, note in enumerate(insights(data), start=1)
    )
    if data.sampled:
        lines.extend(
            [
                "",
                "<i>Метрика применила семплирование; небольшие изменения могут быть неточными.</i>",
            ]
        )
    if monitor_bot_url:
        lines.extend(
            [
                "",
                f'Работает ли сайт и не появился ли noindex: <a href="{html.escape(monitor_bot_url, quote=True)}">бесплатный мониторинг PrivateSEO</a>',
            ]
        )
    lines.extend(["", *[html.escape(note) for note in report_notes(data)]])
    return fit_html("\n".join(lines))


def report_notes(data: ReportData) -> list[str]:
    zone = "МСК" if data.timezone_name == "Europe/Moscow" else data.timezone_name
    notes = [f"Периоды: {zone}. Распознанные Метрикой роботы исключены из всех показателей."]
    if data.pages_partial:
        notes.append(
            "Выгрузка страниц неполная. Показаны только изменения с известными значениями в обоих периодах; отсутствие строки не означает ноль."
        )
    if data.missing_goals:
        notes.append("Некоторые выбранные цели удалены или недоступны. Проверьте выбор: /goals.")
    if data.data_delayed:
        notes.append("Метрика ещё обновляет данные: итог может измениться. Повторите отчёт позже.")
    return notes
