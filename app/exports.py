from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Iterable

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
from matplotlib.figure import Figure
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter, landscape
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


def readings_to_csv(rows: Iterable[tuple[datetime, float | None, str | None]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp_utc", "value", "label"])
    for ts, value, label in rows:
        w.writerow([
            ts.isoformat() if ts else "",
            "" if value is None else value,
            label or "",
        ])
    return buf.getvalue().encode("utf-8")


def render_chart_png(monitor_name: str, unit: str,
                     points: list[tuple[datetime, float]],
                     events: list[tuple[datetime, str]]) -> bytes:
    """Render a chart to PNG using the OO matplotlib API.

    Avoids pyplot's global state, so this is safe under gunicorn threaded workers.
    """
    fig = Figure(figsize=(11, 5))
    ax = fig.subplots()
    if points:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        ax.plot(xs, ys, linewidth=1.0, color="#1f5fa8")
    for ts, label in events:
        ax.axvline(ts, color="#c0392b", linestyle="--", linewidth=0.8, alpha=0.7)
        ax.annotate(label, xy=(ts, 1.0), xycoords=("data", "axes fraction"),
                    xytext=(2, -10), textcoords="offset points",
                    fontsize=8, color="#c0392b", rotation=90,
                    verticalalignment="top")
    ax.set_title(monitor_name)
    ax.set_ylabel(unit)
    ax.set_xlabel("Time (UTC)")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d %H:%M:%S"))
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    return buf.getvalue()


def render_pdf(monitor_name: str, unit: str, start: datetime, end: datetime,
               points: list[tuple[datetime, float]],
               events: list[tuple[datetime, str]],
               summary: dict) -> bytes:
    chart_png = render_chart_png(monitor_name, unit, points, events)

    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out, pagesize=landscape(letter),
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
        title=f"Spot — {monitor_name}",
    )
    styles = getSampleStyleSheet()
    flow = [
        Paragraph(f"<b>Spot — {monitor_name}</b>", styles["Title"]),
        Paragraph(
            f"Range: {start.isoformat()} &mdash; {end.isoformat()} (UTC) &nbsp;|&nbsp; Unit: {unit}",
            styles["Normal"],
        ),
        Spacer(1, 0.15 * inch),
        Image(io.BytesIO(chart_png), width=10 * inch, height=4.5 * inch),
        Spacer(1, 0.2 * inch),
    ]

    rows = [
        ["Samples", str(summary.get("count", 0))],
        ["Min", _fmt(summary.get("min"))],
        ["Max", _fmt(summary.get("max"))],
        ["Average", _fmt(summary.get("avg"))],
        ["Events", str(summary.get("events", 0))],
    ]
    tbl = Table(rows, colWidths=[1.5 * inch, 2.0 * inch])
    tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("FONT", (0, 0), (0, -1), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (0, -1), colors.whitesmoke),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    flow.append(tbl)

    doc.build(flow)
    return out.getvalue()


def _fmt(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.4g}"
    except (TypeError, ValueError):
        return str(v)
