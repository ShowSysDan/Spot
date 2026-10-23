# Changelog

All notable changes to Spot. The version number lives in `app/version.py`
and is bumped with every change.

## 1.1.0 — 2026-10-23

UI overhaul, longer dashboards, and a bug / memory-safety sweep.

### Added
- **Live readout on the Monitors page.** A tile per monitor with the latest
  value, a one-hour sparkline, the 1 h range, and a freshness dot (green /
  amber / grey). Refreshes every 5 s, pauses in hidden tabs. A filter box
  narrows tiles and the Manage table together.
- **Dashboard windows up to 7 days** (1 m … 24 h, 48 h, 7 d) with current /
  min / max / average / samples cards, event markers on the chart, pause,
  "Start Y at 0", and an *Auto* refresh rate that backs off for long windows.
  Window, refresh rate and Y-axis choice are remembered per monitor; the
  window is also kept in the URL.
- **Server-side downsampling** (`max_points=` on `series`, `recent` and
  `overlay`): long ranges come back as bucket averages with min/max, drawn
  as a shaded band. PDF charts use the same mechanism.
- `GET /data/latest` — latest value + sparkline for every monitor in two
  queries.
- `GET /data/overlay/export.csv` — one combined CSV for several monitors
  (replaces the old "open one CSV per monitor in pop-ups", which browsers
  blocked).
- `recent?stats=1` and `overlay?summary=1` for exact window statistics.
- Quick-range presets on Query and Reports (last hour, 6 h, 24 h, 7 days,
  30 days, today, yesterday, this week).
- Copy button for the auth token; Enable/Disable on the monitor detail page;
  **Report** links from the Manage table, detail page and dashboard.
- Version number shown in the footer, logged at startup, and printed in PDF
  footers. Static assets are cache-busted per version.

### Changed
- **"Overlay" is now "Reports"** (`/reports`). `/overlay` redirects with its
  query string intact. The picker is a checkbox list grouped by unit (no more
  Ctrl-click multi-select), and the page shows a per-monitor summary table.
  Selections and range are kept in the URL so reports can be bookmarked.
- Navigation highlights the current section; **+ New monitor** is a button.
- Manage table merges listener/port/thread columns into *Input* and *Status*.
- Overlay/report PDF filenames start `spot_report_` instead of `spot_overlay_`.
- Overlay PDF summary tables use exact SQL aggregates instead of the plotted
  points.
- CSV exports stream from a server-side cursor in 2,000-row chunks.
- Narrow screens: forms wrap and wide tables scroll inside themselves.

### Fixed
- **Python 3.11 / 3.12 crash:** listener, Modbus poller and janitor threads
  stored their stop flag as `self._stop`, shadowing `Thread._stop()`. On
  Debian 12 (Python 3.11) `is_alive()`/`join()` then raised
  `TypeError: 'Event' object is not callable` once a thread had exited,
  breaking the Monitors page and monitor edit / disable / delete.
- **Unbounded TCP threads:** each TCP connection got a thread with no limit.
  Now capped at 32 per listener; stopping a listener also closes its open
  client connections instead of leaving threads alive for up to 60 s.
- **Memory on long exports:** CSV exports loaded every row into memory;
  PDF exports loaded every row just to draw the chart. Both are now bounded.
- **Script injection via names:** delete/purge confirmations embedded the
  monitor or device name inside inline JavaScript, so a name containing a
  quote broke the button (and could run script). Confirmations now read a
  `data-confirm` attribute. The Query page also escaped the unit before
  writing it into HTML.
- **PDF generation failed for names with `&` or `<`** (ReportLab markup);
  `$` in names no longer triggers matplotlib math mode, and a leading `_`
  no longer hides a legend entry.
- Bad `start`/`end`/`minutes`/`seconds` values returned 500; they now return
  400 with a message. `seconds` and ranges are clamped (max 366 days).
- Dashboard used `setInterval` around `fetch`, so slow responses stacked up
  overlapping requests; polling is now sequential, cancels stale requests
  when the window changes, and stops while the tab is hidden.

## 1.0.0

Baseline before versioning was introduced: HTTP/TCP/UDP/Modbus ingest,
per-monitor dashboards, query & export, overlay, retention, storage report.
