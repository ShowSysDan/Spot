from __future__ import annotations

import csv
import io
import os
from datetime import datetime
from typing import Iterable

# Point matplotlib's cache at the install dir (writable under systemd's
# ProtectHome=read-only) before importing matplotlib. setdefault() lets the
# systemd unit override via Environment= if needed.
_SPOT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MPL_CACHE = os.path.join(_SPOT_ROOT, ".cache", "matplotlib")
os.environ.setdefault("MPLCONFIGDIR", _MPL_CACHE)
os.makedirs(os.environ["MPLCONFIGDIR"], exist_ok=True)

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
from matplotlib.figure import Figure

from .util import format_local, naive_local, to_local
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
    """CSV with timestamps in the server's local timezone (ISO-8601 with offset)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "value", "label"])
    for ts, value, label in rows:
        local = to_local(ts)
        w.writerow([
            local.isoformat() if local else "",
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
        xs = [naive_local(p[0]) for p in points]
        ys = [p[1] for p in points]
        ax.plot(xs, ys, linewidth=1.0, color="#1f5fa8")
    for ts, label in events:
        local_ts = naive_local(ts)
        ax.axvline(local_ts, color="#c0392b", linestyle="--", linewidth=0.8, alpha=0.7)
        ax.annotate(label, xy=(local_ts, 1.0), xycoords=("data", "axes fraction"),
                    xytext=(2, -10), textcoords="offset points",
                    fontsize=8, color="#c0392b", rotation=90,
                    verticalalignment="top")
    ax.set_title(monitor_name)
    ax.set_ylabel(unit)
    ax.set_xlabel(f"Time ({_local_tz_name()})")
    ax.grid(True, alpha=0.3)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d %H:%M:%S"))
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    return buf.getvalue()


def _local_tz_name() -> str:
    """Short timezone name for the server (e.g. 'EDT', 'EST', 'UTC')."""
    name = datetime.now().astimezone().tzname()
    return name or "local"


_OVERLAY_COLORS = ("#1f5fa8", "#c0392b", "#27ae60", "#8e44ad",
                   "#d68910", "#16a085", "#2c3e50", "#e91e63")


def render_overlay_chart_png(start: datetime, end: datetime,
                             monitors: list[dict]) -> bytes:
    """Stack one panel per unit; each panel plots all monitors with that unit."""
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for m in monitors:
        u = m["unit"]
        if u not in groups:
            groups[u] = []
            order.append(u)
        groups[u].append(m)

    n = max(1, len(order))
    fig = Figure(figsize=(11, 2.6 * n + 1.5))
    axes = fig.subplots(n, 1, sharex=True, squeeze=False).flatten().tolist()
    color_idx = 0
    for ax, unit in zip(axes, order):
        for m in groups[unit]:
            xs = [naive_local(p["ts"]) for p in m["points"]]
            ys = [p["value"] for p in m["points"]]
            ax.plot(xs, ys, label=m["name"], linewidth=1.0,
                    color=_OVERLAY_COLORS[color_idx % len(_OVERLAY_COLORS)])
            color_idx += 1
        ax.set_ylabel(unit)
        ax.grid(True, alpha=0.3)
        ax.legend(loc="upper right", fontsize=8)
    axes[-1].set_xlabel(f"Time ({_local_tz_name()})")
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d %H:%M:%S"))
    fig.suptitle(f"Spot overlay  ({format_local(start)} — {format_local(end)})", fontsize=11)
    fig.autofmt_xdate()
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    return buf.getvalue()


def render_overlay_pdf(start: datetime, end: datetime, monitors: list[dict]) -> bytes:
    """One-page PDF with the stacked overlay chart and a per-monitor summary table.

    `monitors` is a list of {name, unit, points: [{ts: datetime, value: float}]}.
    """
    chart_png = render_overlay_chart_png(start, end, monitors)
    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out, pagesize=landscape(letter),
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.5 * inch, bottomMargin=0.5 * inch,
        title="Spot — overlay",
    )
    styles = getSampleStyleSheet()
    flow = [
        Paragraph("<b>Spot — overlay</b>", styles["Title"]),
        Paragraph(
            f"Range: {format_local(start)} &mdash; {format_local(end)}",
            styles["Normal"],
        ),
        Spacer(1, 0.15 * inch),
        Image(io.BytesIO(chart_png), width=10 * inch, height=4.5 * inch),
        Spacer(1, 0.2 * inch),
    ]

    rows = [["Monitor", "Unit", "Samples", "Min", "Max", "Avg"]]
    for m in monitors:
        ys = [p["value"] for p in m["points"]]
        if ys:
            rows.append([m["name"], m["unit"], str(len(ys)),
                         _fmt(min(ys)), _fmt(max(ys)), _fmt(sum(ys) / len(ys))])
        else:
            rows.append([m["name"], m["unit"], "0", "—", "—", "—"])
    tbl = Table(rows, colWidths=[2.5 * inch, 1.0 * inch, 1.0 * inch,
                                 1.0 * inch, 1.0 * inch, 1.0 * inch])
    tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 0.5, colors.grey),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("BACKGROUND", (0, 0), (-1, 0), colors.whitesmoke),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    flow.append(tbl)

    doc.build(flow)
    return out.getvalue()


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
            f"Range: {format_local(start)} &mdash; {format_local(end)} &nbsp;|&nbsp; Unit: {unit}",
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
