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
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.graphics.shapes import (
    Circle,
    Drawing,
    Ellipse,
    Rect,
)
from reportlab.platypus import (
    HRFlowable,
    Image,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

try:
    from svglib.svglib import svg2rlg  # type: ignore
except Exception:  # pragma: no cover - graceful fallback if svglib missing
    svg2rlg = None  # type: ignore


# Dark theme palette (mirrors app/static/style.css :root vars).
_BG = colors.HexColor("#0e1116")
_CARD = colors.HexColor("#161b22")
_CARD_2 = colors.HexColor("#1c222b")
_BORDER = colors.HexColor("#2c313a")
_FG = colors.HexColor("#e6e8eb")
_MUTED = colors.HexColor("#8a929d")
_ACCENT = colors.HexColor("#58a6ff")
_DANGER = colors.HexColor("#f85149")

_BG_HEX = "#0e1116"
_CARD_HEX = "#161b22"
_BORDER_HEX = "#2c313a"
_FG_HEX = "#e6e8eb"
_MUTED_HEX = "#8a929d"
_ACCENT_HEX = "#58a6ff"
_DANGER_HEX = "#f85149"

_LOGO_PATH = os.path.join(_SPOT_ROOT, "app", "static", "logo.svg")
_TAGLINE = "Data acquisition, down to the last spot."


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


def _style_dark_axes(ax) -> None:
    ax.set_facecolor(_CARD_HEX)
    for spine in ax.spines.values():
        spine.set_color(_BORDER_HEX)
    ax.tick_params(colors=_FG_HEX, which="both")
    ax.xaxis.label.set_color(_FG_HEX)
    ax.yaxis.label.set_color(_FG_HEX)
    ax.title.set_color(_FG_HEX)
    ax.grid(True, alpha=0.35, color=_BORDER_HEX)


def render_chart_png(monitor_name: str, unit: str,
                     points: list[tuple[datetime, float]],
                     events: list[tuple[datetime, str]]) -> bytes:
    """Render a chart to PNG using the OO matplotlib API.

    Avoids pyplot's global state, so this is safe under gunicorn threaded workers.
    """
    fig = Figure(figsize=(11, 5), facecolor=_BG_HEX)
    ax = fig.subplots()
    _style_dark_axes(ax)
    if points:
        xs = [naive_local(p[0]) for p in points]
        ys = [p[1] for p in points]
        ax.plot(xs, ys, linewidth=1.2, color=_ACCENT_HEX)
    for ts, label in events:
        local_ts = naive_local(ts)
        ax.axvline(local_ts, color=_DANGER_HEX, linestyle="--", linewidth=0.8, alpha=0.8)
        ax.annotate(label, xy=(local_ts, 1.0), xycoords=("data", "axes fraction"),
                    xytext=(2, -10), textcoords="offset points",
                    fontsize=8, color=_DANGER_HEX, rotation=90,
                    verticalalignment="top")
    ax.set_ylabel(unit)
    ax.set_xlabel(f"Time ({_local_tz_name()})")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d %H:%M:%S"))
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, facecolor=fig.get_facecolor())
    return buf.getvalue()


def _local_tz_name() -> str:
    """Short timezone name for the server (e.g. 'EDT', 'EST', 'UTC')."""
    name = datetime.now().astimezone().tzname()
    return name or "local"


# Brighter overlay palette tuned for dark backgrounds.
_OVERLAY_COLORS = ("#58a6ff", "#f85149", "#56d364", "#bc8cff",
                   "#e3b341", "#39c5cf", "#ff7b72", "#ec6cb9")


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
    fig = Figure(figsize=(11, 2.6 * n + 1.5), facecolor=_BG_HEX)
    axes = fig.subplots(n, 1, sharex=True, squeeze=False).flatten().tolist()
    color_idx = 0
    for ax, unit in zip(axes, order):
        _style_dark_axes(ax)
        for m in groups[unit]:
            xs = [naive_local(p["ts"]) for p in m["points"]]
            ys = [p["value"] for p in m["points"]]
            ax.plot(xs, ys, label=m["name"], linewidth=1.2,
                    color=_OVERLAY_COLORS[color_idx % len(_OVERLAY_COLORS)])
            color_idx += 1
        ax.set_ylabel(unit)
        legend = ax.legend(loc="upper right", fontsize=14,
                           facecolor=_CARD_HEX, edgecolor=_BORDER_HEX,
                           labelcolor=_FG_HEX)
        if legend is not None:
            for text in legend.get_texts():
                text.set_color(_FG_HEX)
    axes[-1].set_xlabel(f"Time ({_local_tz_name()})")
    axes[-1].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d %H:%M:%S"))
    fig.autofmt_xdate()
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, facecolor=fig.get_facecolor())
    return buf.getvalue()


def _logo_drawing(height_inch: float = 0.55):
    """Load the app logo as a reportlab Drawing scaled to the given height.

    Tries svglib for a faithful render; falls back to a hand-drawn reportlab
    Drawing so the logo always appears even when svglib isn't installed.
    """
    if svg2rlg is not None and os.path.exists(_LOGO_PATH):
        try:
            d = svg2rlg(_LOGO_PATH)
        except Exception:
            d = None
        if d is not None and d.height:
            s = (height_inch * inch) / d.height
            d.width *= s
            d.height *= s
            d.scale(s, s)
            return d
    return _logo_fallback(height_inch)


def _logo_fallback(height_inch: float = 0.55) -> Drawing:
    """Hand-drawn Spot dalmatian logo using reportlab.graphics primitives.

    Uses no external libraries so the PDF logo works on any deployment.
    SVG coords (y-down) are flipped into reportlab's y-up cartesian system.
    """
    s = (height_inch * inch) / 64.0
    d = Drawing(64 * s, 64 * s)
    body = colors.HexColor("#e6e8eb")
    spot = colors.HexColor("#1a1a1a")
    white = colors.HexColor("#ffffff")

    def x(v: float) -> float:
        return v * s

    def y(v: float) -> float:
        return (64.0 - v) * s

    # Tail
    d.add(Ellipse(x(45), y(45), 4 * s, 2 * s, fillColor=body, strokeColor=None))
    # Front legs (rounded rects)
    d.add(Rect(x(25) - 2 * s, y(60), 4 * s, 9 * s,
               rx=1.5 * s, ry=1.5 * s, fillColor=body, strokeColor=None))
    d.add(Rect(x(35) - 2 * s, y(60), 4 * s, 9 * s,
               rx=1.5 * s, ry=1.5 * s, fillColor=body, strokeColor=None))
    # Body
    d.add(Ellipse(x(32), y(42), 11 * s, 12 * s, fillColor=body, strokeColor=None))
    # Body spots (paint before head so head sits on top cleanly)
    for cx, cy, r in [
        (25, 35, 1.6), (32, 35, 1.5), (38, 36, 1.5),
        (24, 41, 1.5), (32, 42, 1.7), (39, 43, 1.4),
        (28, 47, 1.5), (35, 47, 1.6), (30, 52, 1.4),
        (38, 50, 1.3), (26, 50, 1.3),
    ]:
        d.add(Circle(x(cx), y(cy), r * s, fillColor=spot, strokeColor=None))
    # Head + snout
    d.add(Ellipse(x(32), y(20), 9 * s, 8 * s, fillColor=body, strokeColor=None))
    d.add(Ellipse(x(32), y(27), 5.5 * s, 4 * s, fillColor=body, strokeColor=None))
    # Head spot
    d.add(Circle(x(32), y(13), 1.4 * s, fillColor=spot, strokeColor=None))
    # Ears (dark, droopy ovals)
    d.add(Ellipse(x(23.5), y(20), 3 * s, 5.5 * s, fillColor=spot, strokeColor=None))
    d.add(Ellipse(x(40.5), y(20), 3 * s, 5.5 * s, fillColor=spot, strokeColor=None))
    # Eyes + catchlights
    d.add(Circle(x(28.5), y(18), 1.9 * s, fillColor=spot, strokeColor=None))
    d.add(Circle(x(35.5), y(18), 1.9 * s, fillColor=spot, strokeColor=None))
    d.add(Circle(x(27.9), y(17.4), 0.6 * s, fillColor=white, strokeColor=None))
    d.add(Circle(x(34.9), y(17.4), 0.6 * s, fillColor=white, strokeColor=None))
    # Nose
    d.add(Ellipse(x(32), y(26), 3 * s, 2 * s, fillColor=spot, strokeColor=None))
    return d


def _dark_styles():
    """Paragraph styles tinted for the dark page background."""
    base = getSampleStyleSheet()
    title = ParagraphStyle("DarkTitle", parent=base["Title"],
                           fontName="Helvetica-Bold",
                           textColor=_FG, alignment=0,
                           fontSize=18, leading=21,
                           spaceBefore=0, spaceAfter=1)
    body = ParagraphStyle("DarkBody", parent=base["Normal"],
                          textColor=_FG, fontSize=10, leading=13)
    muted = ParagraphStyle("DarkMuted", parent=base["Normal"],
                           textColor=_MUTED, fontSize=9.5, leading=12)
    section = ParagraphStyle("DarkSection", parent=base["Normal"],
                             fontName="Helvetica-Bold",
                             textColor=_ACCENT, fontSize=8.5, leading=11,
                             spaceBefore=0, spaceAfter=1)
    return title, body, muted, section


def _section_label(text: str, section_style) -> Paragraph:
    """Tracked-out uppercase section label tinted with the accent color.

    Spaces letters within each word and uses non-breaking spaces between
    words so ReportLab's whitespace collapse doesn't fuse the words.
    """
    words = [" ".join(w) for w in text.upper().split()]
    return Paragraph("&nbsp;&nbsp;&nbsp;&nbsp;".join(words), section_style)


def _accent_rule(color=_ACCENT, thickness: float = 1.5,
                 space_before: float = 3, space_after: float = 6) -> HRFlowable:
    return HRFlowable(width="100%", thickness=thickness, color=color,
                      lineCap="round",
                      spaceBefore=space_before, spaceAfter=space_after)


def _brand_block() -> Table:
    """Logo + SPOT wordmark + tagline lockup that anchors every report."""
    logo = _logo_drawing(0.7)
    wordmark = Paragraph(
        f'<font name="Helvetica-Bold" size="22" color="{_FG_HEX}">'
        f'<b>S P O T</b></font>',
        ParagraphStyle("Wordmark", fontName="Helvetica-Bold", fontSize=22,
                       textColor=_FG, leading=24, alignment=0),
    )
    tagline = Paragraph(
        f'<font name="Helvetica-Oblique" size="9.5" color="{_MUTED_HEX}">'
        f'<i>{_TAGLINE}</i></font>',
        ParagraphStyle("Tagline", fontName="Helvetica-Oblique", fontSize=9.5,
                       textColor=_MUTED, leading=12, alignment=0,
                       spaceBefore=1),
    )
    block = Table(
        [[logo, [wordmark, tagline]]],
        colWidths=[0.85 * inch, None],
    )
    block.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (1, 0), (1, 0), 12),
    ]))
    return block


def _meta_line(pairs: list[tuple[str, str]]) -> Paragraph:
    """Metadata row: 'LABEL value · LABEL value · …' with muted/foreground tinting."""
    parts = []
    for label, value in pairs:
        parts.append(
            f'<font color="{_MUTED_HEX}" size="8.5"><b>{label.upper()}</b></font>'
            f' &nbsp;<font color="{_FG_HEX}" size="10">{value}</font>'
        )
    sep = f' &nbsp;&nbsp;<font color="{_BORDER_HEX}">|</font>&nbsp;&nbsp; '
    html = sep.join(parts)
    return Paragraph(
        html,
        ParagraphStyle("Meta", fontName="Helvetica", fontSize=10, leading=14,
                       textColor=_FG, alignment=0),
    )


def _draw_dark_page(canvas, doc) -> None:
    """Page chrome: dark background + footer rule, tagline, and page number."""
    canvas.saveState()
    w, h = doc.pagesize
    canvas.setFillColor(_BG)
    canvas.rect(0, 0, w, h, fill=1, stroke=0)

    # Footer rule
    canvas.setStrokeColor(_BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(0.5 * inch, 0.5 * inch, w - 0.5 * inch, 0.5 * inch)

    # Footer left: brand + tagline
    canvas.setFillColor(_MUTED)
    canvas.setFont("Helvetica-Oblique", 8.5)
    canvas.drawString(0.5 * inch, 0.32 * inch,
                      f"Spot  ·  {_TAGLINE}")

    # Footer right: generated stamp + page number
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    canvas.setFillColor(_MUTED)
    canvas.setFont("Helvetica", 8.5)
    canvas.drawRightString(w - 0.5 * inch, 0.32 * inch,
                           f"Generated {stamp}   ·   Page {doc.page}")
    canvas.restoreState()


def _stat_tile(label: str, value: str, value_pt: int = 22,
               primary: bool = False) -> Table:
    """Stat card: small muted label above a large bold value.

    `primary=True` adds an accent-colored top edge so headline metrics stand out.
    """
    label_p = Paragraph(
        f'<font color="{_MUTED_HEX}" size="9"><b>{"  ".join(label.upper())}</b></font>',
        ParagraphStyle("StatLabel", fontName="Helvetica-Bold", alignment=1,
                       leading=11, textColor=_MUTED),
    )
    value_p = Paragraph(
        f'<font color="{_FG_HEX}" size="{value_pt}"><b>{value}</b></font>',
        ParagraphStyle("StatValue", fontName="Helvetica-Bold", alignment=1,
                       leading=value_pt + 2, textColor=_FG),
    )
    tile = Table([[label_p], [value_p]], colWidths=[None])
    cmds = [
        ("BACKGROUND", (0, 0), (-1, -1), _CARD_2 if primary else _CARD),
        ("BOX", (0, 0), (-1, -1), 0.5, _BORDER),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (0, 0), 6),
        ("BOTTOMPADDING", (0, 0), (0, 0), 1),
        ("TOPPADDING", (0, 1), (0, 1), 0),
        ("BOTTOMPADDING", (0, 1), (0, 1), 8),
    ]
    if primary:
        cmds.append(("LINEABOVE", (0, 0), (-1, 0), 2.5, _ACCENT))
    tile.setStyle(TableStyle(cmds))
    return tile


def _dark_table_style(header_row: bool = True, font_size: int = 9) -> TableStyle:
    pad = max(4, font_size // 2)
    cmds = [
        ("BOX", (0, 0), (-1, -1), 0.5, _BORDER),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, _BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TEXTCOLOR", (0, 0), (-1, -1), _FG),
        ("BACKGROUND", (0, 0), (-1, -1), _CARD_2),
        ("FONT", (0, 0), (-1, -1), "Helvetica", font_size),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), pad),
        ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
    ]
    if header_row:
        cmds.extend([
            ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", font_size),
            ("BACKGROUND", (0, 0), (-1, 0), _CARD),
            ("TEXTCOLOR", (0, 0), (-1, 0), _ACCENT),
        ])
    return TableStyle(cmds)


def _join_monitor_names(monitors: list[dict], char_cap: int = 60) -> str:
    """'Foo, Bar + 2 more' style title summarizing the monitors in a report."""
    names = [m.get("name", "") for m in monitors if m.get("name")]
    if not names:
        return "Report"
    out = ""
    for i, n in enumerate(names):
        candidate = (out + ", " if out else "") + n
        if len(candidate) > char_cap and i > 0:
            return f"{out} + {len(names) - i} more"
        out = candidate
    return out


def render_overlay_pdf(start: datetime, end: datetime, monitors: list[dict]) -> bytes:
    """Branded one-page PDF: stacked overlay chart + per-monitor summary table.

    `monitors` is a list of {name, unit, points: [{ts: datetime, value: float}]}.
    """
    chart_png = render_overlay_chart_png(start, end, monitors)
    out = io.BytesIO()
    doc = SimpleDocTemplate(
        out, pagesize=landscape(letter),
        leftMargin=0.5 * inch, rightMargin=0.5 * inch,
        topMargin=0.35 * inch, bottomMargin=0.55 * inch,
        title="Spot report",
    )
    title_style, _body, _muted, section_style = _dark_styles()

    units = sorted({m.get("unit", "") for m in monitors if m.get("unit")})
    flow = [
        _brand_block(),
        _accent_rule(),
        Paragraph(f"<b>{_join_monitor_names(monitors)}</b>", title_style),
        _meta_line([
            ("Range", f"{format_local(start)} &nbsp;&rarr;&nbsp; {format_local(end)}"),
            ("Monitors", str(len(monitors))),
            ("Units", ", ".join(units) if units else "—"),
        ]),
        Spacer(1, 0.08 * inch),
        _section_label("Timeseries", section_style),
        Image(io.BytesIO(chart_png), width=10 * inch, height=4.0 * inch),
        Spacer(1, 0.08 * inch),
        _section_label("Per-monitor summary", section_style),
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
    tbl.setStyle(_dark_table_style(header_row=True, font_size=12))
    flow.append(tbl)

    doc.build(flow, onFirstPage=_draw_dark_page, onLaterPages=_draw_dark_page)
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
        topMargin=0.35 * inch, bottomMargin=0.55 * inch,
        title=f"Spot — {monitor_name}",
    )
    title_style, _body, _muted, section_style = _dark_styles()

    flow = [
        _brand_block(),
        _accent_rule(),
        Paragraph(f"<b>{monitor_name}</b>", title_style),
        _meta_line([
            ("Range", f"{format_local(start)} &nbsp;&rarr;&nbsp; {format_local(end)}"),
            ("Unit", unit),
        ]),
        Spacer(1, 0.08 * inch),
        _section_label("Timeseries", section_style),
        Image(io.BytesIO(chart_png), width=10 * inch, height=4.0 * inch),
        Spacer(1, 0.08 * inch),
        _section_label("Summary", section_style),
    ]

    stat_row = Table(
        [[
            _stat_tile("Samples", str(summary.get("count", 0)), value_pt=16),
            _stat_tile("Min",     _fmt(summary.get("min")),     value_pt=24, primary=True),
            _stat_tile("Max",     _fmt(summary.get("max")),     value_pt=24, primary=True),
            _stat_tile("Average", _fmt(summary.get("avg")),     value_pt=24, primary=True),
            _stat_tile("Events",  str(summary.get("events", 0)), value_pt=16),
        ]],
        colWidths=[1.5 * inch, 2.05 * inch, 2.05 * inch, 2.05 * inch, 1.5 * inch],
    )
    stat_row.setStyle(TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    flow.append(stat_row)

    doc.build(flow, onFirstPage=_draw_dark_page, onLaterPages=_draw_dark_page)
    return out.getvalue()


def _fmt(v) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.4g}"
    except (TypeError, ValueError):
        return str(v)
