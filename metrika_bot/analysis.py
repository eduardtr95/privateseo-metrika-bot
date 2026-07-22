from __future__ import annotations

import html
import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

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


@dataclass
class ReportData:
    counter_name: str
    current_period: Period
    previous_period: Period
    visits: Change
    users: Change
    goals: Change | None
    goal_names: list[str]
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


def _meaningful(change: Change, min_previous: float, percent: float, absolute: float) -> bool:
    return bool(
        change.percent is not None
        and change.previous >= min_previous
        and abs(change.percent) >= percent
        and abs(change.absolute) >= absolute
    )


def insights(data: ReportData) -> list[str]:
    notes: list[str] = []
    visits = data.visits
    goals = data.goals

    if goals and _meaningful(goals, 5, 25, 2) and goals.absolute < 0:
        if not _meaningful(visits, 30, 15, 10):
            notes.append(
                "🔴 Целей стало заметно меньше при стабильном трафике. Проверьте формы, телефоны и первый экран ключевых страниц."
            )
        else:
            notes.append(
                "🔴 Цели просели вместе с трафиком — сначала найдите источник потери визитов."
            )

    if _meaningful(visits, 30, 15, 10):
        if visits.absolute < 0:
            losses = sorted(
                (item for item in data.sources if item.delta < 0), key=lambda x: x.delta
            )
            if losses:
                lead = losses[0]
                share = abs(lead.delta) / abs(visits.absolute) if visits.absolute else 0
                if share >= 0.45:
                    notes.append(
                        f"🟡 Главная потеря — «{lead.name}»: {_number(lead.previous)} → {_number(lead.current)} визитов."
                    )
            page_losses = sorted(
                (item for item in data.pages if item.delta < 0), key=lambda x: x.delta
            )
            if page_losses:
                lead_page = page_losses[0]
                share = abs(lead_page.delta) / abs(visits.absolute) if visits.absolute else 0
                if share >= 0.35:
                    notes.append(
                        f"Проверьте страницу {_short_page(lead_page.name)} — она дала большую часть падения."
                    )
            if not notes:
                notes.append(
                    "🟡 Трафик снизился сразу в нескольких местах — откройте источники и посадочные страницы."
                )
        else:
            notes.append(
                "🟢 Трафик заметно вырос. Зафиксируйте источник роста и масштабируйте удачные страницы."
            )

    if goals and visits.percent is not None and goals.percent is not None:
        if visits.percent >= 20 and goals.previous >= 5 and goals.percent <= 0:
            notes.append(
                "Трафик вырос, а цели — нет: проверьте качество нового источника и соответствие посадочных страниц."
            )

    if not notes:
        notes.append("🟢 Существенных изменений, требующих реакции, не найдено.")
    return notes[:3]


def format_report(data: ReportData, monitor_bot_url: str | None = None) -> str:
    period = data.current_period
    lines = [
        f"<b>Неделя сайта {html.escape(data.counter_name)}</b>",
        f"{period.start.strftime('%d.%m')}–{period.end.strftime('%d.%m.%Y')} против предыдущих 7 дней",
        "",
        f"👥 <b>Визиты:</b> {_change(data.visits)}",
        f"👤 <b>Посетители:</b> {_change(data.users)}",
    ]
    if data.goals:
        lines.append(f"🎯 <b>Выбранные цели:</b> {_change(data.goals)}")
    else:
        lines.append("🎯 <b>Цели не выбраны.</b> Нажмите «Настроить цели», чтобы видеть заявки.")
    lines.extend(["", "<b>Что требует внимания</b>"])
    lines.extend(html.escape(note) for note in insights(data))

    movers = sorted(data.pages, key=lambda item: abs(item.delta), reverse=True)
    movers = [item for item in movers if abs(item.delta) >= 3][:3]
    if movers:
        lines.extend(["", "<b>Страницы с наибольшим изменением</b>"])
        for item in movers:
            sign = "+" if item.delta > 0 else ""
            lines.append(
                f"• {html.escape(_short_page(item.name))}: {sign}{_number(item.delta)} визитов"
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
