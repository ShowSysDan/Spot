# Changelog

All notable changes to Spot. The version number lives in `app/version.py`
and is bumped with every change.

## 1.5.0 — 2026-10-24

### Added
- **Scheduled reports** (Setup → Scheduled reports): daily / weekly /
  monthly e-mailed PDFs covering the previous complete Eastern-time period;
  per-schedule monitors, recipients, send time (24-hour) and weekday; *Send
  now*; last-run status. A scheduler thread checks every 30 s; missed runs
  are caught up once after downtime; DST-correct periods.
- **Wall display** (`/wall`): full-screen kiosk view — large auto-sized
  values, sparklines, alert colours, 24-hour Eastern clock, location filter,
  full-screen toggle (F), idle cursor hiding, screen wake lock where
  available. Linked from the Monitors page.

## 1.4.0 — 2026-10-24

### Added
- **Shows**: named time ranges with a set of monitors (`/shows`, new, edit,
  detail page with levels table + chart, PDF/CSV, delete). Reports gains
  *Load a show…* and *Save as show…*.
- **Automatic show reports**: after a show ends (+5 min grace) Spot e-mails
  the PDF to `email_to` and/or POSTs it with JSON statistics
  (multipart/form-data) to `callback_url`; retries with exponential backoff,
  up to 6 attempts; *Send now* on the show page.
- **External API `/api/v1`** with API keys (Setup → API keys; SHA-256 hashed,
  shown once, disable/delete, last-used): `GET /`, `/monitors`, `/stats`,
  `/series`, `/report.pdf`, and shows CRUD with upsert by `external_id`,
  per-show PDF and `deliver`. JSON errors throughout.
- **Acoustic statistics**: Leq (energy-equivalent level), L10, L50, L90 for
  units starting with "dB", plus p10/p50/p90 for every monitor — in the API,
  show pages, Reports summary and PDF tables.
- **docs/API.md**: the complete integration guide for 321Theater and other
  systems.
- **`SPOT_TIMEZONE`** (default `America/New_York`): one display zone, 24-hour,
  for every time people see. Setup shows it beside the server's own zone.

### Changed
- **All times are US Eastern, 24-hour**, regardless of the viewer's
  computer: chart axes and tooltips (Chart.js now uses the Luxon date
  adapter with a fixed zone; date-fns adapter removed, Luxon vendored),
  date pickers (date + 24-hour time box instead of the browser's 12-hour
  control), quick ranges (Eastern midnights), summaries, PDFs, CSVs,
  e-mails, download file names and API responses (ISO-8601 with offset).
  Previously browser-side times followed each viewer's zone and locale.
- Navigation: Monitors · Shows · Reports · Alerts · Setup.
- Query helpers moved to `app/queries.py`, request parsing to
  `app/routes/params.py` (shared by the web data endpoints and the API).
- Report PDFs accept a title and extra metadata, and add Leq / L10 / L90
  columns when dB monitors are included.
- Alert webhook timestamps use the Eastern offset like everything else.

## 1.3.0 — 2026-10-24

### Added
- **Alerts.** Per-monitor *above* / *below* thresholds judged on a
  configurable rolling average, plus a *no data for N seconds* alert. A
  background engine (`SPOT_ALERT_INTERVAL_SECONDS`, default 15 s) opens one
  alert per crossing, tracks its worst value and closes it on recovery.
  Alerts are shown on tiles (red glow / amber when acknowledged), as a
  dashboard banner with threshold lines on the chart, on a new **Alerts**
  page (active + acknowledge, configured monitors, history) and as a menu
  badge. Every raise/recovery goes to syslog, and optionally to e-mail
  (`SPOT_SMTP_*`, `SPOT_ALERT_EMAIL_TO`) and a JSON webhook
  (`SPOT_ALERT_WEBHOOK_URL`).
- **Location** label on monitors; the Monitors page groups tiles by it.
- **Per-monitor colour**, used for its tile, dashboard line, Reports line
  and PDF charts. Monitors without one get a stable colour from the palette.
- **Setup** page gathering Modbus devices, Storage and Alerts, with the
  current notification settings and *Send test e-mail / webhook* buttons.
- `SPOT_PUBLIC_URL` for links in e-mails and webhooks.
- New tables/columns are added automatically on start (`alerts` table;
  `monitors.location`, `color`, `alert_high`, `alert_low`,
  `alert_window_seconds`, `alert_stale_seconds`).

### Changed
- Navigation is now Monitors · Reports · Alerts · Setup; Modbus and Storage
  moved under Setup (their URLs are unchanged).
- The Modbus fieldset on the monitor form uses shared styles instead of inline
  CSS.

## 1.2.0 — 2026-10-24

### Changed
- **Fully offline / internal-network capable.** Chart.js 4.4.4 and
  chartjs-adapter-date-fns 3.0.0 are vendored in `app/static/vendor/`
  (checksums and upgrade steps in its README) instead of loaded from
  jsDelivr. Previously every chart failed on a network without internet.
- New `scripts/build-wheelhouse.sh` for installing the Python dependencies
  on an offline server.
- The push examples on a monitor's page show this server's real address
  instead of `HOST:WEB_PORT`.

## 1.1.1 — 2026-10-23

Cleanup release: code that had grown by repeated patching was rewritten in
one consistent shape. Behaviour, URLs, form messages and the database
layout are unchanged unless listed under *Fixed*.

### Changed (internal)
- `app/workers.py` — new `WorkerRegistry` holds the start / stop / shutdown /
  status bookkeeping that `ListenerManager` and `ModbusManager` each had a
  copy of.
- `app/db.py` — the stack of hand-written `ALTER TABLE … ADD COLUMN IF NOT
  EXISTS` statements is now a declarative `_ADDED_COLUMNS` table (plus
  `_ALTERED_COLUMNS` for type changes) applied in a loop. New columns are
  one appended line.
- `app/routes/web.py` — monitor and Modbus forms validate through small
  helpers (`_form_str`, `_form_int`, `_check`) raising one `_FormError`,
  replacing 19 copies of "flash + redirect". New/edit pages share one
  renderer each; TCP/UDP port assignment is its own `_assign_port()`, and a
  save now uses one database session instead of three.
- `app/routes/api.py` — JSON and form-encoded ingest share one field parser.
- `app/exports.py` — each PDF colour is defined once; both PDF reports are
  laid out by one `_build_report()`; the overlay summary always uses the SQL
  aggregates (the points-based fallback was unreachable).
- Removed dead code: `janitor.get_default_retention()`,
  `ModbusManager.restart_device()`, `data._fetch_rows()`, unused imports.

### Fixed
- A JSON ingest body that isn't an object (e.g. `[1]`) returned 500 over
  HTTP; it now returns 400 `malformed payload`. The same payload over
  TCP/UDP/plain text is logged as malformed instead of raising.
- Report PDFs with more than three monitors spilled the last summary row onto
  a second page. The table and chart now shrink to keep up to ~6 monitors on
  one page, and the table header repeats if it still overflows.
- PDF charts draw a dot per reading when a series has fewer than 40, so a
  monitor with a single sample is no longer invisible.

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
