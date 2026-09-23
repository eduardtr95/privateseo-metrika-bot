"""Calendar views, personal source selection and ephemeral chart data."""

from __future__ import annotations

import calendar
import html
from dataclasses import dataclass, field
from datetime import date, timedelta
import json

from .analysis import Change, Period, ReportData, _compact_value, source_name

SOURCE_LABELS = {
    "organic": "Поиск",
    "direct": "Прямые заходы",
    "ad": "Реклама",
    "referral": "Ссылки с сайтов",
    "social": "Соцсети",
    "messenger": "Мессенджеры",
    "email": "Рассылки",
    "recommend": "Рекомендации",
    "qrcode": "QR-коды",
    "internal": "Внутренние переходы",
    "saved": "Сохранённые страницы",
    "external": "Внешние переходы",
    "undefined": "Не определено",
}
MONTHS = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
MONTH_TITLES = (
    "",
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)
MODES = {"day": "День", "week": "Неделя", "month": "Месяц"}


def source_selection(connection) -> list[str] | None:
    raw = dict(connection).get("visible_sources")
    return None if raw is None else [key for key in json.loads(raw) if key in SOURCE_LABELS]


def month_before(day: date) -> date:
    return (day.replace(day=1) - timedelta(days=1)).replace(day=1)


def calendar_periods(mode: str, today: date) -> tuple[Period, Period, str]:
    yesterday = today - timedelta(days=1)
    if mode == "day":
        return (
            Period(yesterday, yesterday),
            Period(yesterday - timedelta(days=1), yesterday - timedelta(days=1)),
            "Вчера",
        )
    if mode == "week":
        start = today - timedelta(days=today.weekday())
        if today == start:
            start -= timedelta(days=7)
            title = "Прошлая неделя"
        else:
            title = "Эта неделя"
        return (
            Period(start, yesterday),
            Period(start - timedelta(days=7), yesterday - timedelta(days=7)),
            title,
        )
    if mode != "month":
        raise ValueError("Invalid report view")
    start = today.replace(day=1) if today.day > 1 else yesterday.replace(day=1)
    previous_start = month_before(start)
    last = calendar.monthrange(previous_start.year, previous_start.month)[1]
    return (
        Period(start, yesterday),
        Period(previous_start, previous_start.replace(day=min(yesterday.day, last))),
        MONTH_TITLES[start.month],
    )


def human_period(period: Period, year: bool = False) -> str:
    end = f"{period.end.day} {MONTHS[period.end.month]}"
    if period.start != period.end:
        start = str(period.start.day)
        if period.start.month != period.end.month or period.start.year != period.end.year:
            start += f" {MONTHS[period.start.month]}"
        if period.start.year != period.end.year:
            start += f" {period.start.year}"
        end = f"{start}–{end}"
    if year:
        end += f" {period.end.year}"
    return end


@dataclass
class Dashboard:
    mode: str
    title: str
    selected_sources: list[str] | None
    source_ids: dict[str, str] = field(default_factory=dict)
    dates: list[date] = field(default_factory=list)
    series: dict[str, list[float | None]] = field(default_factory=dict)
    goal_series: list[float | None] = field(default_factory=list)
    chart_warning: str | None = None
    sampled: bool = False
    chart_enabled: bool = True


def collect_dashboard(builder, chat_id, connection, today, mode, *, chart=True, include_pages=True):
    current, previous, title = calendar_periods(mode, today)
    data = builder.collect(
        chat_id, connection, today=today, periods=(current, previous), include_pages=include_pages
    )
    dash = Dashboard(mode, title, source_selection(connection), source_ids=data.source_ids)
    data.dashboard = dash
    dash.chart_enabled = bool(chart and dict(connection).get("chart_enabled", 1))
    if dash.chart_enabled:
        ensure_history(builder, chat_id, connection, data)
    return data


def ensure_history(builder, chat_id, connection, data):
    dash = data.dashboard
    if dash.dates and not dash.chart_warning:
        return
    dash.dates, dash.series, dash.goal_series = [], {}, []
    dash.chart_warning, dash.sampled = None, False
    try:
        collect_history(builder.yandex, chat_id, int(connection["counter_id"]), data)
    except Exception as exc:
        from .yandex import YandexAPIError

        if isinstance(exc, YandexAPIError) and exc.reconnect:
            raise
        dash.chart_warning = "График временно недоступен; цифры отчёта получены."


def collect_history(yandex, chat_id, counter_id, data):
    dash = data.dashboard
    end = data.current_period.end
    start = min(data.previous_period.start, end - timedelta(days=13))
    dash.dates = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    date_index = {day.isoformat(): i for i, day in enumerate(dash.dates)}

    def rows(metrics, dimensions, filters=None):
        output = []
        complete = True
        for offset in range(1, 5001, 500):
            payload = yandex.report(
                chat_id,
                counter_id,
                start.isoformat(),
                end.isoformat(),
                metrics,
                dimensions,
                limit=500,
                offset=offset,
                filters=filters,
            )
            dash.sampled |= bool(payload.get("sampled"))
            chunk = payload.get("data", [])
            output.extend(chunk)
            total = int(payload.get("total_rows", len(output)))
            if len(output) >= total:
                break
            if not chunk or offset == 4501:
                complete = False
                break
        if not complete:
            raise ValueError("Incomplete chart data")
        return output

    if dash.selected_sources != []:
        filters = None
        if dash.selected_sources is not None:
            filters = " OR ".join(f"ym:s:trafficSource=='{key}'" for key in dash.selected_sources)
        traffic = rows(["ym:s:visits"], ["ym:s:date", "ym:s:trafficSource"], filters)
        selected = (
            dash.selected_sources
            if dash.selected_sources is not None
            else list(dict.fromkeys(str(row["dimensions"][1]["id"]) for row in traffic))
        )
        for key in selected:
            dash.series[key] = [0.0] * len(dash.dates)
        for row in traffic:
            dimensions = row["dimensions"]
            day, key = (
                str(dimensions[0].get("id") or dimensions[0]["name"]),
                str(dimensions[1]["id"]),
            )
            if day not in date_index:
                raise ValueError("Unexpected history date")
            if key in dash.series:
                raw = row["metrics"][0]
                dash.series[key][date_index[day]] = None if raw is None else float(raw)

    if data.valid_goal_ids:
        filters = " OR ".join(f"ym:s:goal{goal}IsReached=='yes'" for goal in data.valid_goal_ids)
        goals = rows(["ym:s:visits"], ["ym:s:date"], filters)
        dash.goal_series = [0.0] * len(dash.dates)
        for row in goals:
            day = str(row["dimensions"][0].get("id") or row["dimensions"][0]["name"])
            if day not in date_index:
                raise ValueError("Unexpected history date")
            raw = row["metrics"][0]
            dash.goal_series[date_index[day]] = None if raw is None else float(raw)


def visible_sources(data):
    dash = data.dashboard
    selected = None if dash is None else dash.selected_sources
    items = data.sources
    if selected is not None:
        items = [item for item in items if data.source_ids.get(item.name) in selected]
    return sorted(items, key=lambda item: (-item.current, -item.previous, item.name))


def dashboard_text(data: ReportData, goal_limit: int = 3) -> str:
    dash = data.dashboard
    include_year = data.current_period.end.year != data.previous_period.start.year
    current = human_period(data.current_period, include_year)
    previous = human_period(data.previous_period, include_year)
    cur_days = (data.current_period.end - data.current_period.start).days + 1
    prev_days = (data.previous_period.end - data.previous_period.start).days + 1

    def value(change, small_base=20):
        if cur_days == prev_days:
            return _compact_value(change, small_base)
        # February and other short months: compare pace, not unequal totals.
        rate = Change(change.current / cur_days, change.previous / prev_days)
        if not change.previous:
            delta = "было 0"
        else:
            delta = f"{rate.percent:+.0f}% в день"
        return f"<b>{round(change.current):,}</b> · {delta}".replace(",", " ")

    lines = [
        f"<b>{html.escape(data.counter_name[:70])}</b>",
        f"<b>{dash.title}: {current}</b>",
        f"Сравнение: {previous}",
        "",
        f"Всего визитов: {value(data.visits)}",
        f"Посетители: {value(data.users)}",
    ]
    sources = visible_sources(data)
    if dash.selected_sources == []:
        pass
    elif sources:
        lines.extend(["", "<b>Источники · визиты</b>"])
        for item in sources:
            if not item.current and not item.previous:
                continue
            key = data.source_ids.get(item.name)
            label = SOURCE_LABELS.get(key, source_name(item.name))
            lines.append(f"{html.escape(label[:32])}: {value(Change(item.current, item.previous))}")
    else:
        lines.append("В выбранных источниках визитов нет.")
    if data.goals is not None and data.goal_names:
        lines.extend(["", f"<b>Целевые визиты:</b> {value(data.goals, 5)}"])
        for goal in data.goal_details[:goal_limit]:
            label = goal.name if len(goal.name) <= 36 else goal.name[:35] + "…"
            lines.append(f"{html.escape(label)}: {value(Change(goal.current, goal.previous), 5)}")
        if len(data.goal_details) > goal_limit:
            lines.append(f"Ещё {len(data.goal_details) - goal_limit} целей — в подробностях.")
    elif data.goal_names:
        lines.append("⚠️ Данные целей недоступны → /goals")
    elif not data.missing_goals:
        lines.append("Цели не выбраны → /goals")
    if cur_days != prev_days:
        lines.append(f"Δ по среднему за день: периоды {cur_days} и {prev_days} дн.")
    if data.missing_goals:
        lines.append("⚠️ Некоторые цели недоступны → /goals")
    if data.sampled or dash.sampled:
        lines.append("⚠️ Выборочные данные: изменения приблизительны.")
    if data.data_delayed:
        lines.append("⚠️ Метрика ещё обновляет данные.")
    if dash.chart_enabled and dash.chart_warning:
        lines.append(dash.chart_warning)
    lines.extend(["", "<i>МСК · без роботов. Итоги и цели — весь сайт.</i>"])
    return "\n".join(lines)


def dashboard_details(data):
    from .analysis import _report_movers, _page_label, _number, insights, report_notes
    from .formatting import fit_html

    lines = [dashboard_text(data, goal_limit=15)]
    _, losses, gains = _report_movers(data)
    for title, pages in (("Страницы: потери", losses), ("Страницы: рост", gains)):
        if pages:
            lines.extend(["", f"<b>{title} · все источники</b>", "Визиты: было → стало"])
            for item in pages:
                lines.append(
                    f'<a href="{html.escape(item.name, quote=True)}">{html.escape(_page_label(item.name))}</a>: {_number(item.previous)} → {_number(item.current)}'
                )
    if data.goals is not None:
        lines.extend(
            [
                "",
                "По отдельным целям — число достижений; целевые визиты — визиты хотя бы с одной целью без дублей.",
            ]
        )
    if (data.current_period.end - data.current_period.start) == (
        data.previous_period.end - data.previous_period.start
    ):
        lines.extend(["", "<b>Что проверить</b>", *[html.escape(note) for note in insights(data)]])
    lines.extend(["", *[html.escape(note) for note in report_notes(data)]])
    return fit_html("\n".join(lines))
