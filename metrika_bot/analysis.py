from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlparse

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


def completed_weeks(today: date | None = None) -> tuple[Period, Period]:
    today = today or date.today()
    current_end = today - timedelta(days=1)
    current = Period(current_end - timedelta(days=6), current_end)
    previous_end = current.start - timedelta(days=1)
    previous = Period(previous_end - timedelta(days=6), previous_end)
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
    current: dict[str, float], previous: dict[str, float]
) -> list[BreakdownChange]:
    return [
        BreakdownChange(name, current.get(name, 0), previous.get(name, 0))
        for name in set(current) | set(previous)
    ]


class ReportBuilder:
    def __init__(self, yandex: YandexClient):
        self.yandex = yandex

    def collect(self, chat_id: int, connection: Any, today: date | None = None) -> ReportData:
        counter_id = int(connection["counter_id"])
        goal_ids = [int(value) for value in json.loads(connection["goal_ids"] or "[]")]
        goals = self.yandex.goals(chat_id, counter_id)
        goal_map = {int(goal["id"]): str(goal.get("name") or goal["id"]) for goal in goals}
        selected = [goal_id for goal_id in goal_ids if goal_id in goal_map][:15]
        metrics = ["ym:s:visits", "ym:s:users"] + [
            f"ym:s:goal{goal_id}reaches" for goal_id in selected
        ]
        current, previous = completed_weeks(today)

        cur_total = self.yandex.report(
            chat_id, counter_id, current.api_start, current.api_end, metrics
        )
        prev_total = self.yandex.report(
            chat_id, counter_id, previous.api_start, previous.api_end, metrics
        )
        cur_values, prev_values = _totals(cur_total), _totals(prev_total)
        cur_goals = sum(cur_values[2:]) if selected else None
        prev_goals = sum(prev_values[2:]) if selected else None
        goal_details = [
            BreakdownChange(goal_map[goal_id], cur_values[index], prev_values[index])
            for index, goal_id in enumerate(selected, start=2)
        ]

        source_dimension = ["ym:s:trafficSource"]
        page_dimension = ["ym:s:startURL"]
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
        cur_pages = self.yandex.report(
            chat_id,
            counter_id,
            current.api_start,
            current.api_end,
            ["ym:s:visits"],
            page_dimension,
            limit=500,
        )
        prev_pages = self.yandex.report(
            chat_id,
            counter_id,
            previous.api_start,
            previous.api_end,
            ["ym:s:visits"],
            page_dimension,
            limit=500,
        )
        sampled = any(
            payload.get("sampled") is True
            for payload in (cur_total, prev_total, cur_sources, prev_sources, cur_pages, prev_pages)
        )
        return ReportData(
            counter_name=str(connection["counter_name"] or counter_id),
            current_period=current,
            previous_period=previous,
            visits=Change(cur_values[0], prev_values[0]),
            users=Change(cur_values[1], prev_values[1]),
            goals=Change(cur_goals, prev_goals) if cur_goals is not None else None,
            goal_names=[goal_map[goal_id] for goal_id in selected],
            goal_details=goal_details,
            sources=compare_breakdowns(_breakdown(cur_sources), _breakdown(prev_sources)),
            pages=compare_breakdowns(_breakdown(cur_pages), _breakdown(prev_pages)),
            sampled=sampled,
        )


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
}


def source_name(value: str) -> str:
    return SOURCE_NAMES.get(value, value)


def goal_relevance(name: str) -> int:
    """2 = primary business goal, 1 = contact intent, 0 = auxiliary goal."""
    lowered = name.casefold()
    if "youtube" in lowered or "ютуб" in lowered or "канал" in lowered:
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
        f" · {_signed(item.delta)} ({_signed_percent(item.percent)})"
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
        and (item.percent is None or abs(item.percent) >= 20)
    )


def _meaningful(change: Change, min_previous: float, percent: float, absolute: float) -> bool:
    return bool(
        change.percent is not None
        and change.previous >= min_previous
        and abs(change.percent) >= percent
        and abs(change.absolute) >= absolute
    )


def insights(data: ReportData) -> list[str]:
    notes: list[str] = []
    source_losses = sorted(
        (item for item in data.sources if item.delta < 0 and _important(item)),
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


def format_report(data: ReportData, monitor_bot_url: str | None = None) -> str:
    period = data.current_period
    lines = [
        f"<b>{html.escape(data.counter_name)}: что изменилось за неделю</b>",
        f"{period.start.strftime('%d.%m')}–{period.end.strftime('%d.%m.%Y')} против предыдущих 7 дней",
        "",
        "<b>Итог</b>",
    ]
    lines.extend(_summary(data))
    lines.append(f"Посетители: {_change(data.users)}")

    source_movers = sorted(data.sources, key=lambda item: abs(item.delta), reverse=True)
    source_movers = [item for item in source_movers if abs(item.delta) >= 3][:4]
    if source_movers:
        lines.extend(["", "<b>Почему изменился итог</b>"])
        lines.extend(_mover_line(item, source_name(item.name)) for item in source_movers)

    page_losses = sorted(
        (item for item in data.pages if item.delta < 0 and _important(item)),
        key=lambda item: item.delta,
    )[:3]
    page_gains = sorted(
        (item for item in data.pages if item.delta > 0 and _important(item)),
        key=lambda item: item.delta,
        reverse=True,
    )[:3]
    if page_losses:
        lines.extend(["", "<b>Посадочные страницы: наибольшие потери</b>"])
        lines.extend(_mover_line(item, _page_label(item.name), item.name) for item in page_losses)
    if page_gains:
        lines.extend(["", "<b>Посадочные страницы: наибольший рост</b>"])
        lines.extend(_mover_line(item, _page_label(item.name), item.name) for item in page_gains)
    if page_losses or page_gains:
        lines.append(
            "<i>До 3 страниц в каждом блоке: изменение от 3 визитов и от 20%. "
            "Сравниваются до 500 самых посещаемых страниц каждого периода.</i>"
        )

    lines.extend(["", "<b>Бизнес-действия</b>"])
    selected_business = [name for name in data.goal_names if goal_relevance(name) > 0]
    selected_auxiliary = [name for name in data.goal_names if goal_relevance(name) == 0]
    if data.goals and selected_business:
        lines.append(f"Всего: {_change(data.goals)}")
        for item in data.goal_details[:5]:
            lines.append(_mover_line(item, item.name))
        if selected_auxiliary:
            names = ", ".join(f"«{name}»" for name in selected_auxiliary)
            lines.append(f"ℹ️ В сумму также входят вспомогательные цели: {html.escape(names)}.")
    elif data.goal_names:
        names = ", ".join(f"«{name}»" for name in data.goal_names)
        lines.append(
            f"⚠️ Сейчас выбрано только {html.escape(names)} — это не заявка. "
            "Выберите заявки, телефон, email, мессенджер или чат."
        )
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
    return "\n".join(lines)[:4096]
