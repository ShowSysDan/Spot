from __future__ import annotations

import csv
import io
import os
import statistics
from datetime import datetime
from xml.sax.saxutils import escape as _xml_escape

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

from . import glossary
from .units import is_sound_unit
from .util import naive_local, now_local, to_local
from .version import __version__
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
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
    KeepInFrame,
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


# Dark theme palette (mirrors app/static/style.css :root vars). Hex strings
# feed matplotlib and ReportLab <font> markup; the HexColor twins feed
# ReportLab table/canvas styling.
_BG_HEX = "#0e1116"
_CARD_HEX = "#161b22"
_BORDER_HEX = "#2c313a"
_FG_HEX = "#e6e8eb"
_MUTED_HEX = "#8a929d"
_ACCENT_HEX = "#58a6ff"

_BG, _BORDER, _FG, _MUTED, _ACCENT = (
    colors.HexColor(h) for h in (_BG_HEX, _BORDER_HEX, _FG_HEX, _MUTED_HEX, _ACCENT_HEX)
)

_LOGO_PATH = os.path.join(_SPOT_ROOT, "app", "static", "logo.svg")
_TAGLINE = "Data acquisition, down to the last spot."


def iter_csv(rows, include_monitor: bool = False, chunk_rows: int = 2000):
    """Yield CSV text in chunks, timestamps in the server's local timezone
    (ISO-8601 with offset). Rows are (ts, value, label) or, with
    include_monitor, (ts, monitor_name, unit, value, label).

    Lines are buffered `chunk_rows` at a time so a streamed export never
    holds more than one chunk in memory.
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["timestamp", "monitor", "unit", "value", "label"] if include_monitor
               else ["timestamp", "value", "label"])
    n = 0
    for row in rows:
        ts, *lead, value, label = row
        local = to_local(ts)
        w.writerow([local.isoformat() if local else "", *lead,
                    "" if value is None else value, label or ""])
        n += 1
        if n >= chunk_rows:
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate()
            n = 0
    yield buf.getvalue()


def _style_axes(ax) -> None:
    """Each panel is a shaded, outlined box (the card colour of the web UI)
    with a faint grid, so the chart reads as one framed figure."""
    ax.set_facecolor(_CARD_HEX)
    for spine in ax.spines.values():
        spine.set_color(_BORDER_HEX)
        spine.set_linewidth(0.8)
    ax.tick_params(colors=_MUTED_HEX, labelsize=7, length=2)
    ax.yaxis.label.set_color(_FG_HEX)
    ax.yaxis.label.set_fontsize(8)
    ax.grid(True, color=_BORDER_HEX, linewidth=0.5)
    ax.set_axisbelow(True)


def _time_axis(ax, start: datetime, end: datetime) -> None:
    """Horizontal time labels: 'HH:MM' within one day, with the date once in
    the axis title; date-aware concise labels for longer ranges."""
    a, b = to_local(start), to_local(end)
    ax.set_xlim(naive_local(start), naive_local(end))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=5, maxticks=10))
    zone = _zone_label(start, end)
    if a.date() == b.date():
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        title = f"{a:%a %d %b %Y} · {zone} (24-hour)"
    else:
        ax.xaxis.set_major_formatter(
            mdates.ConciseDateFormatter(ax.xaxis.get_major_locator(), show_offset=False))
        title = f"{a:%a %d %b} – {b:%a %d %b %Y} · {zone} (24-hour)"
    ax.set_xlabel(title, color=_MUTED_HEX, fontsize=7)


def render_report_chart_png(start: datetime, end: datetime, monitors: list[dict],
                            events: list[tuple[datetime, str]],
                            width_in: float, height_in: float) -> bytes:
    """One panel per unit, sharing the time axis; event markers as dashed
    lines labelled along the top. Monitors are {name, unit, color, points};
    a lone monitor with downsampled points also gets its min/max band and
    shaded data gaps. Sized to the exact space it fills on the page.

    Uses the OO matplotlib API (no pyplot state), so it is safe under
    gunicorn threaded workers.
    """
    units: list[str] = []
    for m in monitors:
        if m["unit"] not in units:
            units.append(m["unit"])
    units = units or [""]
    # Sound panels carry the story of a show, so they get twice the height.
    ratios = [2 if is_sound_unit(u) else 1 for u in units] if len(units) > 1 else [1]
    fig = Figure(figsize=(width_in, height_in), facecolor=_BG_HEX)
    axes = fig.subplots(len(units), 1, sharex=True, squeeze=False,
                        gridspec_kw={"height_ratios": ratios}).flatten().tolist()
    single = len(monitors) == 1
    for ax, unit in zip(axes, units):
        _style_axes(ax)
        for m in (m for m in monitors if m["unit"] == unit):
            pts = m["points"]
            xs = [naive_local(p["ts"]) for p in pts]
            gaps = _gap_after([p["ts"] for p in pts])
            if single:
                _shade_gaps(ax, xs, gaps)
                if pts and "min" in pts[0]:
                    bx, lo, hi = _split_at_gaps(xs, [p["min"] for p in pts],
                                                [p["max"] for p in pts], gaps=gaps)
                    ax.fill_between(bx, lo, hi, color=m["color"], alpha=0.18, linewidth=0)
            px, ys = _split_at_gaps(xs, [p["value"] for p in pts], gaps=gaps)
            ax.plot(px, ys, color=m["color"], linewidth=0.9, **_sparse_markers(xs))
        ax.set_ylabel(_mpl_text(unit))
        for ts, _label in events:
            ax.axvline(naive_local(ts), color=_MUTED_HEX, linestyle=(0, (3, 2)), linewidth=0.7)
    _label_events(axes[0], start, end, events)
    _time_axis(axes[-1], start, end)
    fig.tight_layout(pad=0.3, h_pad=0.8)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=200, facecolor=fig.get_facecolor())
    return buf.getvalue()


def _label_events(ax, start: datetime, end: datetime,
                  events: list[tuple[datetime, str]]) -> None:
    """Label event lines along the top of the panel, skipping any label that
    would crowd the previous one (its line is still drawn)."""
    span = (end - start).total_seconds() or 1
    last = None
    for ts, label in events:
        frac = (ts - start).total_seconds() / span
        if last is not None and frac - last < 0.09:
            continue
        last = frac
        ax.annotate(_mpl_text(label)[:24], xy=(naive_local(ts), 1.0),
                    xycoords=("data", "axes fraction"), xytext=(2, -2),
                    textcoords="offset points", fontsize=6.5, color=_MUTED_HEX, va="top")


def _gap_after(ts: list[datetime]) -> list[int]:
    """Indices i where the pause from ts[i] to ts[i+1] is a gap: over 5× the
    median spacing and at least 30 s (same rule as spot.js findGaps).

    Uses the real (aware) timestamps, so the hour skipped when clocks spring
    forward isn't mistaken for missing data.
    """
    if len(ts) < 3:
        return []
    deltas = [(b - a).total_seconds() for a, b in zip(ts, ts[1:])]
    limit = max(5 * statistics.median(deltas), 30.0)
    return [i for i, d in enumerate(deltas) if d > limit]


def _split_at_gaps(xs: list, *series: list, gaps: list[int]) -> tuple[list, ...]:
    """Insert NaN after each gap start so matplotlib breaks the line there."""
    if not gaps:
        return (xs, *series)
    cut = set(gaps)
    out_x, outs = [], [[] for _ in series]
    for i, x in enumerate(xs):
        out_x.append(x)
        for o, ys in zip(outs, series):
            o.append(ys[i])
        if i in cut:
            out_x.append(x)
            for o in outs:
                o.append(float("nan"))
    return (out_x, *outs)


def _shade_gaps(ax, xs: list, gaps: list[int]) -> None:
    for i in gaps:
        a, b = xs[i], xs[i + 1]
        ax.axvspan(a, b, facecolor=_MUTED_HEX, alpha=0.10, hatch="///",
                   edgecolor=_BORDER_HEX, linewidth=0)
        ax.annotate("no data", xy=(a + (b - a) / 2, 0.02), xycoords=("data", "axes fraction"),
                    ha="center", va="bottom", fontsize=7, color=_MUTED_HEX)


def _sparse_markers(xs: list) -> dict:
    """Dot each reading when there are only a few, so a lone sample (which
    would be a zero-length line) is still visible."""
    return {"marker": "o", "markersize": 3} if len(xs) < 40 else {}


def _mpl_text(s: str) -> str:
    """Make user text literal for matplotlib: '$' would start mathtext and a
    leading '_' hides a legend entry."""
    s = (s or "").replace("$", r"\$")
    return " " + s if s.startswith("_") else s


def _zone_label(start: datetime | None, end: datetime | None) -> str:
    """Zone abbreviation(s) for the plotted range — 'EDT', or 'EDT/EST' when
    it spans a daylight-saving change — not the zone at generation time."""
    names = []
    for dt in (start, end):
        n = to_local(dt).tzname() if dt else None
        if n and n not in names:
            names.append(n)
    return "/".join(names) or (now_local().tzname() or "local")


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


# ---------------------------------------------------------------------------
# PDF report: one portrait page — header, chart, measured values, calculated
# sound values, notes. Written for non-technical readers: every column says
# in a few words what it is, and the notes explain the rest.
# ---------------------------------------------------------------------------

_PAGE_W, _PAGE_H = letter
_MARGIN = 0.5 * inch
_CONTENT_W = _PAGE_W - 2 * _MARGIN   # content lines up with the footer rule
_TOP, _BOTTOM = 0.45 * inch, 0.6 * inch
_FRAME_PAD = 6                       # SimpleDocTemplate's frame padding (pt)
_CHART_MIN_IN, _CHART_MAX_IN = 1.8, 5.8

# Columns of the two tables: (summary key, heading).
_MEASURED_COLS = (("count", "Samples"), ("min", "Min"), ("max", "Max"),
                  ("avg", "Average"), ("events", "Events"))
_CALCULATED_COLS = (("leq", "Leq"), ("l10", "L10"), ("l90", "L90"))
# Terms that need more than their column label; explained under Notes.
_NOTE_KEYS = ("max", "leq", "l10", "l90")


def _styles() -> dict[str, ParagraphStyle]:
    def st(name, size, leading, font="Helvetica", color=_FG, align=0):
        return ParagraphStyle(name, fontName=font, fontSize=size, leading=leading,
                              textColor=color, alignment=align)
    return {
        "brand": st("Brand", 20, 22, "Helvetica-Bold"),
        "tagline": st("Tagline", 8.5, 11, "Helvetica-Oblique", _MUTED),
        "when": st("When", 16, 19, "Helvetica-Bold", align=2),
        "when_small": st("WhenSmall", 8.5, 11, color=_MUTED, align=2),
        "title": st("Title", 14, 17, "Helvetica-Bold"),
        "meta": st("Meta", 9, 12, color=_MUTED),
        "section": st("Section", 10.5, 13, "Helvetica-Bold"),
        "intro": st("Intro", 8, 10, color=_MUTED),
        "cell": st("Cell", 8.5, 10.5),
        "head": st("Head", 8.5, 10, "Helvetica-Bold"),
        "head_r": st("HeadR", 8.5, 10, "Helvetica-Bold", align=2),
        "sub": st("Sub", 6.8, 8, color=_MUTED),
        "sub_r": st("SubR", 6.8, 8, color=_MUTED, align=2),
        "key": st("Key", 7.6, 9.6),
        "note": st("Note", 7.4, 9.4, "Helvetica-Oblique", _MUTED),
        "legend": st("Legend", 7.6, 10, color=_MUTED),
    }


def _draw_page(canvas, doc) -> None:
    """Dark background and a one-line footer."""
    canvas.saveState()
    canvas.setFillColor(_BG)
    canvas.rect(0, 0, _PAGE_W, _PAGE_H, fill=1, stroke=0)
    canvas.setStrokeColor(_BORDER)
    canvas.setLineWidth(0.5)
    canvas.line(_MARGIN, 0.45 * inch, _PAGE_W - _MARGIN, 0.45 * inch)
    canvas.setFillColor(_MUTED)
    canvas.setFont("Helvetica", 7)
    canvas.drawString(_MARGIN, 0.3 * inch, f"Spot v{__version__}")
    stamp = now_local().strftime("%Y-%m-%d %H:%M %Z")
    canvas.drawRightString(_PAGE_W - _MARGIN, 0.3 * inch, f"Generated {stamp}")
    canvas.restoreState()


def _duration(start: datetime, end: datetime) -> str:
    mins = round((end - start).total_seconds() / 60)
    days, mins = divmod(mins, 24 * 60)
    hours, mins = divmod(mins, 60)
    parts = [f"{days} d" if days else "", f"{hours} h" if hours else "",
             f"{mins} min" if mins or not (days or hours) else ""]
    return " ".join(p for p in parts if p)


def _range_lines(start: datetime, end: datetime) -> tuple[str, str, str]:
    """(small line above, big line, small line below) for the header's
    time-range block: 'Sat 24 Oct 2026' / '19:00 – 21:45 EDT' / '2 h 45 min'
    within one day; the dates big and the times small across days."""
    a, b = to_local(start), to_local(end)
    if a.date() == b.date():
        return f"{a:%a %d %b %Y}", f"{a:%H:%M} – {b:%H:%M} {b.tzname()}", _duration(start, end)
    big = (f"{a:%d %b} – {b:%d %b %Y}" if a.year == b.year
           else f"{a:%d %b %Y} – {b:%d %b %Y}")
    return (f"{a:%a %H:%M} → {b:%a %H:%M} {b.tzname()}", big, _duration(start, end))


def _header(title: str | None, start: datetime, end: datetime, n_monitors: int,
            extra_meta: list[tuple[str, str]], ss) -> list:
    """Brand lockup on the left, the time range large on the right, then
    the report's own title (a show name; omitted otherwise, since the chart
    key names the monitors) and a line of details."""
    brand = [Paragraph("<b>Spot</b>", ss["brand"]), Paragraph(_TAGLINE, ss["tagline"])]
    above, big, below = _range_lines(start, end)
    when = [Paragraph(above, ss["when_small"]), Paragraph(f"<b>{big}</b>", ss["when"]),
            Paragraph(below, ss["when_small"])]
    top = Table([[_logo_drawing(0.5), brand, when]],
                colWidths=[0.65 * inch, None, 3.6 * inch])
    top.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    flow = [top, HRFlowable(width="100%", thickness=1.2, color=_ACCENT,
                            spaceBefore=6, spaceAfter=5)]
    if title:
        flow.append(Paragraph(f"<b>{_xml_escape(title)}</b>", ss["title"]))
    details = [f"{n_monitors} monitor{'' if n_monitors == 1 else 's'}"]
    details += [f"{_xml_escape(label)}: <font color='{_FG_HEX}'>{_xml_escape(value)}</font>"
                for label, value in extra_meta]
    flow += [Paragraph(" &nbsp;·&nbsp; ".join(details), ss["meta"]), Spacer(1, 6)]
    return flow


def _swatch(m: dict, ss) -> Paragraph:
    return Paragraph(f'<font color="{m["color"]}">■</font>&nbsp; {_xml_escape(m["name"])}',
                     ss["cell"])


def _heading_cell(key: str | None, label: str, ss, right: bool = True) -> list:
    cell = [Paragraph(label, ss["head_r" if right else "head"])]
    if key:
        cell.append(Paragraph(glossary.SHORT[key], ss["sub_r" if right else "sub"]))
    return cell


def _values_table(monitors: list[dict], cols, ss, bold_first: bool = False) -> Table:
    rows = [[_heading_cell(None, "Monitor", ss, right=False),
             _heading_cell(None, "Unit", ss, right=False),
             *(_heading_cell(k, label, ss) for k, label in cols)]]
    for m in monitors:
        s = m["summary"]
        rows.append([_swatch(m, ss), _xml_escape(m["unit"]),
                     *(f"{s.get(k) or 0:,}" if k in ("count", "events") else _fmt(s.get(k))
                       for k, _label in cols)])
    name_w, unit_w = 2.3 * inch, 0.6 * inch
    col_w = (_CONTENT_W - name_w - unit_w) / len(cols)
    tbl = Table(rows, colWidths=[name_w, unit_w] + [col_w] * len(cols), repeatRows=1)
    cmds = [
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, _FG),
        ("LINEBELOW", (0, 1), (-1, -2), 0.3, _BORDER),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("VALIGN", (0, 0), (-1, 0), "BOTTOM"),
        ("ALIGN", (2, 1), (-1, -1), "RIGHT"),
        ("FONT", (0, 1), (-1, -1), "Helvetica", 8.5),
        ("TEXTCOLOR", (0, 0), (-1, -1), _FG),
        ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]
    if bold_first:
        cmds.append(("FONT", (2, 1), (2, -1), "Helvetica-Bold", 8.5))
    tbl.setStyle(TableStyle(cmds))
    return tbl


def _section(title: str, intro: str, ss) -> list:
    return [Paragraph(title, ss["section"]), Paragraph(_xml_escape(intro), ss["intro"]),
            Spacer(1, 3)]


def _notes(keys: list[str], ss) -> list:
    flow = [Paragraph("Notes", ss["section"]), Spacer(1, 2)]
    for k in _NOTE_KEYS:
        if k in keys:
            label, text = glossary.STATS[k]
            flow.append(Paragraph(f"<b>{label}</b> — {_xml_escape(text)}", ss["key"]))
    if any(k in keys for k in glossary.ACOUSTIC):
        flow += [Spacer(1, 2), Paragraph(_xml_escape(glossary.DB_NOTE), ss["note"])]
    return flow


def _chart_legend(monitors: list[dict], ss) -> Paragraph:
    """Colour key above the chart; entries wrap as whole units."""
    def entry(m):
        text = _xml_escape(f'{m["name"]} ({m["unit"]})').replace(" ", "&nbsp;")
        return f'<font color="{m["color"]}">■</font>&nbsp;{text}'
    return Paragraph(" &nbsp;&nbsp; ".join(entry(m) for m in monitors), ss["legend"])


def _render_report(title: str | None, start: datetime, end: datetime, monitors: list[dict],
                   events: list[tuple[datetime, str]], doc_title: str,
                   extra_meta: list[tuple[str, str]] | None = None) -> bytes:
    """Lay out the one-page report. `monitors` are {name, unit, color,
    points, summary}; summaries carrying `leq` (sound units) also fill the
    calculated-values table. The chart gets whatever height the rest leaves;
    if even its minimum doesn't fit (many monitors), the page is scaled down
    rather than spilling onto a second one."""
    ss = _styles()
    measured_title, measured_intro, _ = glossary.GROUPS[0]
    calc_title, calc_intro, _ = glossary.GROUPS[1]
    sound = [m for m in monitors if "leq" in m["summary"]]

    before = [*_header(title, start, end, len(monitors), extra_meta or [], ss),
              Paragraph("Levels over time", ss["section"]), _chart_legend(monitors, ss),
              Spacer(1, 2)]
    after = [Spacer(1, 8),
             *_section(measured_title, measured_intro, ss),
             _values_table(monitors, _MEASURED_COLS, ss), Spacer(1, 10)]
    keys = [k for k, _ in _MEASURED_COLS]
    if sound:
        after += [*_section(calc_title, calc_intro, ss),
                  _values_table(sound, _CALCULATED_COLS, ss, bold_first=True), Spacer(1, 10)]
        keys += list(glossary.ACOUSTIC)
    after += _notes(keys, ss)

    avail = _PAGE_H - _TOP - _BOTTOM - 2 * _FRAME_PAD
    used = sum(f.wrap(_CONTENT_W, avail)[1] + f.getSpaceBefore() + f.getSpaceAfter()
               for f in before + after)
    chart_in = min(_CHART_MAX_IN, max(_CHART_MIN_IN, (avail - used) / inch - 0.05))
    chart = Image(io.BytesIO(render_report_chart_png(start, end, monitors, events,
                                                     _CONTENT_W / inch, chart_in)),
                  width=_CONTENT_W, height=chart_in * inch)

    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=letter, title=doc_title,
                            # The frame pads its content by _FRAME_PAD, so pull
                            # the margins out by that much.
                            leftMargin=_MARGIN - _FRAME_PAD,
                            rightMargin=_MARGIN - _FRAME_PAD,
                            topMargin=_TOP, bottomMargin=_BOTTOM)
    doc.build([KeepInFrame(_CONTENT_W, avail, before + [chart] + after, mode="shrink")],
              onFirstPage=_draw_page)
    return out.getvalue()


def _report_events(monitors: list[dict]) -> list[tuple[datetime, str]]:
    """Event markers from all monitors, oldest first, duplicates dropped."""
    seen = {(e["ts"], e["label"]) for m in monitors for e in m.get("events", [])}
    return sorted(seen, key=lambda e: e[0])


def render_overlay_pdf(start: datetime, end: datetime, monitors: list[dict],
                       title: str | None = None,
                       extra_meta: list[tuple[str, str]] | None = None) -> bytes:
    """One-page PDF report for several monitors.

    `monitors` is a list of {name, unit, color, points: [{ts, value, ...}],
    summary, events?: [{ts, label}]}, where `summary` holds exact SQL
    aggregates (points may be downsampled). Summaries with acoustic figures
    (`leq`, see queries.compute_summary) fill the calculated-values table.
    `title` (e.g. a show name) heads the report; without one the chart key
    names the monitors. `extra_meta` adds (label, plain text) details.
    """
    return _render_report(title, start, end, monitors, _report_events(monitors),
                          title or "Spot report", extra_meta)


def render_pdf(monitor_name: str, unit: str, start: datetime, end: datetime,
               points: list[dict],
               events: list[tuple[datetime, str]],
               summary: dict, color: str = _ACCENT_HEX) -> bytes:
    """One-page PDF report for a single monitor (same layout as the overlay)."""
    monitor = {"name": monitor_name, "unit": unit, "color": color,
               "points": points, "summary": summary}
    return _render_report(None, start, end, [monitor], events, f"Spot — {monitor_name}")


def _fmt(v) -> str:
    """One decimal for everyday magnitudes (92.2 dBA, 72.0 °F); three
    significant figures for small values so 0.0253 doesn't print as 0.0."""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return f"{f:,.1f}" if abs(f) >= 10 else f"{f:.3g}"
