"""Render small chart PNGs in memory; no analytics or images are persisted."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
import math
import os

from .dashboard import SOURCE_LABELS, human_period
from .analysis import Period

COLORS = ("#2878E8", "#E49D2A", "#8B5BD1", "#D25177", "#3D9C99", "#778296")


def font(size):
    from PIL import ImageFont

    paths = [
        os.environ.get("CHART_FONT", ""),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    for path in paths:
        if path and Path(path).is_file():
            return ImageFont.truetype(path, size)
    raise RuntimeError("Install fonts-dejavu-core or set CHART_FONT")


def render_chart(data) -> bytes | None:
    from PIL import Image, ImageDraw

    dash = data.dashboard
    if not dash or dash.chart_warning or not dash.dates:
        return None
    traffic = [(SOURCE_LABELS.get(key, key), values) for key, values in dash.series.items()]
    traffic.sort(key=lambda pair: -sum(v or 0 for v in pair[1]))
    if len(traffic) > 5:
        others = [
            None
            if any(values[i] is None for _, values in traffic[4:])
            else sum(values[i] for _, values in traffic[4:])
            for i in range(len(dash.dates))
        ]
        traffic = traffic[:4] + [("Остальные выбранные", others)]
    panels = []
    if traffic:
        panels.append(("Визиты по источникам", traffic))
    if dash.goal_series:
        panels.append(("Целевые визиты · любая выбранная цель", [("Без дублей", dash.goal_series)]))
    if not panels:
        return None
    width = 1000
    panel_height = 310
    height = 112 + panel_height * len(panels)
    image = Image.new("RGB", (width, height), "#FFFFFF")
    draw = ImageDraw.Draw(image)
    small, regular, title = font(23), font(27), font(31)
    draw.text((36, 20), "Динамика по дням", fill="#142237", font=title)
    draw.text(
        (36, 61), human_period(Period(dash.dates[0], dash.dates[-1])), fill="#6A7789", font=regular
    )
    for panel, (heading, series) in enumerate(panels):
        top = 110 + panel * panel_height
        draw.text((36, top), heading, fill="#142237", font=regular)
        left, right, ytop, bottom = 82, 953, top + 105, top + 248
        actual_max = max(
            (v for _, values in series for v in values if v is not None and math.isfinite(v)),
            default=0,
        )
        step = max(1, math.ceil(actual_max / 3))
        maximum = step * 3
        xstep = (right - left) / max(1, len(dash.dates) - 1)
        highlighted = next(
            (i for i, day in enumerate(dash.dates) if day >= data.current_period.start),
            len(dash.dates),
        )
        if highlighted < len(dash.dates):
            draw.rectangle((left + highlighted * xstep, ytop, right, bottom), fill="#F1F6FE")
        for tick in range(4):
            y = bottom - tick / 3 * (bottom - ytop)
            draw.line((left, y, right, y), fill="#E7EBF1", width=1)
            draw.text((left - 13, y), str(step * tick), fill="#718094", font=small, anchor="rm")
        for index, (label, values) in enumerate(series):
            color = (
                "#23A47A"
                if heading.startswith("Целевые")
                else COLORS[list(SOURCE_LABELS.values()).index(label) % len(COLORS)]
                if label in SOURCE_LABELS.values()
                else "#778296"
            )
            short = label if len(label) <= 19 else label[:18] + "…"
            legend_x = 36 + (index % 3) * 322
            legend_y = top + 53 + (index // 3) * 29
            draw.line((legend_x, legend_y, legend_x + 19, legend_y), fill=color, width=4)
            draw.text((legend_x + 25, legend_y - 14), short, font=small, fill="#536175")
            previous = None
            for i, value in enumerate(values):
                if value is None or not math.isfinite(value):
                    previous = None
                    continue
                point = (left + i * xstep, bottom - max(0, value) / maximum * (bottom - ytop))
                if previous is not None:
                    draw.line((previous, point), fill=color, width=4)
                draw.ellipse((point[0] - 2, point[1] - 2, point[0] + 2, point[1] + 2), fill=color)
                previous = point
        indices = sorted({0, (len(dash.dates) - 1) // 2, len(dash.dates) - 1})
        for index in indices:
            draw.text(
                (left + index * xstep, bottom + 13),
                dash.dates[index].strftime("%d.%m"),
                fill="#718094",
                font=small,
                anchor="mt",
            )
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
