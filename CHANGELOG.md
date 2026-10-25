# Changelog

All notable changes to Spot. The version number lives in `app/version.py`
and is bumped with every change.

## 1.10.0 — 2026-10-25

### Added
- **Plain-language key to the summary figures**. PDF reports gain a
  *How to read this report* page explaining Samples, Min, Max, Average,
  Events and, for dB monitors, Leq, L10 and L90, plus a note on what a
  difference in dB sounds like, so the PDF can go to non-technical readers.
  The show page and Reports page show the same key (*What these numbers
  mean*) under their summary tables. The wording lives in `app/glossary.py`.

### Changed
- The Reports PDF summary column reads *Average* instead of *Avg*.

## 1.9.0 — 2026-10-24

### Added
- **Setup → Logging & syslog** (`/setup/logging`), applied live without a
  restart and stored in the database (overrides `.env`; *Reset to .env*):
  syslog on/off, destination (local socket or `host:port`), **UDP or TCP**,
  **app name** (syslog tag), facility; one level per log category (Alerts,
  API requests, Security, Data ingest, UI changes, Show reports & webhooks,
  Listeners & Modbus, Exports, System); syslog status (connected, sent,
  failed, last error) and *Send test message*; a filterable view of the last
  500 log lines.
- **API access log**: one line per `/api/v1` request — method, path,
  status, duration, size, API key name, source IP (4xx warning, 5xx error).
- **Source IP** (`[src=…]`) on every line logged while handling a web or API
  request, so UI changes and API calls are attributable.
- `spot.security` logger for rejected API keys, refused cross-site posts and
  ingest from outside `SPOT_INGEST_ALLOW`; export logging for CSV/PDF
  downloads.
- New `.env` settings `SPOT_SYSLOG_PROTOCOL` and `SPOT_SYSLOG_APP_NAME`;
  new `settings` table (created automatically).

### Fixed
- An unusable syslog destination (e.g. `/dev/log` missing) was ignored
  silently; it's now reported at start-up and on the Logging page.
- Syslog over TCP never reconnected after the collector restarted, and lost
  the first line after a disconnect without noticing. Spot's own sender
  frames lines RFC 3164 with timestamp and hostname, reconnects, detects a
  closed connection before writing, and counts undeliverable lines.

## 1.8.0 — 2026-10-24

### Removed
- **All e-mail sending.** SMTP settings (`SPOT_SMTP_*`), alert e-mails
  (`SPOT_ALERT_EMAIL_TO`), show report e-mail (`email_to` in the UI and
  API) and the *Send test e-mail* button are gone. **Scheduled reports**
  (Setup → Scheduled reports), which only ever sent e-mail, are removed too.
  Alerts still go to the log/syslog and the alert webhook; show reports
  still go to their callback URL and can be downloaded at any time.
  Existing databases keep the now-unused `report_schedules` /
  `schedule_monitors` tables and `shows.email_to` column untouched (nothing
  reads them); drop them by hand if you like:
  `DROP TABLE spot.schedule_monitors, spot.report_schedules;
  ALTER TABLE spot.shows DROP COLUMN email_to;`
  Old `SPOT_SMTP_*` lines left in `.env` are simply ignored.

### Added
- **Loading feedback** for slow requests: a spinner overlay with a
  description and elapsed time on Reports, Query, show pages and dashboard
  window changes (buttons disabled meanwhile), and a *Preparing…* notice
  for PDF/CSV downloads that clears when the file starts arriving.

### Changed
- **Faster long ranges.** Statistics are computed in one pass instead of
  six queries (percentiles share one sort; extremes found by an index walk),
  the raw-vs-downsampled check stops counting at `max_points`, and bucketing
  avoids slow NUMERIC arithmetic. For 30 days of 1 Hz data: Reports preview
  4.4 s → 2.3 s, chart data 2.3 s → 0.95 s, PDF 4.2 s → 2.5 s. Results
  unchanged (`min_at` / `max_at` are now always the *first* occurrence).

## 1.7.1 — 2026-10-24

Fixes from a full bug / memory review of 1.2–1.7.

### Fixed
- **A show or schedule name containing a line break could stop all report
  delivery**: building the e-mail raised outside the error handling, the
  attempt was never recorded, and the same show retried (and blocked the
  rest of the queue) every minute. E-mail subjects are now forced onto one
  line, message-building errors are caught, every show / schedule / alert
  notification is isolated so one failure can't stop the others, and names
  are collapsed to one line on input (API and web forms).
- **"Send now" during a show cancelled the real post-show report.** A
  manual send before the report is due is now a *preview* (`"final": false`
  in the callback); the final report still goes out after the show.
- **Callback URLs**: redirects are no longer followed; link-local /
  reserved addresses (e.g. `169.254.169.254`) are refused; URLs are
  re-checked at send time; new optional `SPOT_CALLBACK_ALLOW` host/CIDR
  allow-list.
- **Cross-site request forgery**: web-form POSTs whose Origin/Referer is
  another site are refused (403).
- Open-redirect hole in the `next` parameter (`/\evil.com`).
- API show validation: wrong types now give 400 instead of 500 or being
  accepted (`callback_url`, `email_to`, `name`, `notes`); e-mail addresses
  are checked properly; `POST /shows` upsert now really replaces the show
  (omitted optional fields are cleared); concurrent creates with the same
  `external_id` no longer 500; `external_id` is trimmed the same way on
  store and lookup.
- More than 50 monitor ids is a 400 instead of being silently trimmed.
- Detection: `detect_monitor` may now be any monitor (not only one in the
  request) and an unknown id is a 404; the default reference follows the
  order the monitors were listed in.
- Scheduled reports no longer count a reading at exactly midnight in two
  consecutive reports.
- PDF charts: axis label shows the zone of the plotted range (EDT/EST)
  rather than the zone at generation time; gaps are computed on real
  timestamps so the spring-forward hour isn't hatched as missing.
- Dashboard: changing the window during a fetch now refreshes as soon as
  that fetch ends (it could wait up to a minute). Reports: an older preview
  can no longer overwrite a newer one.
- Shutdown waits for the alert, report and schedule threads to finish their
  current pass before closing the database.
- The web *Send now* route reports an error for a show with no recipients
  instead of marking it delivered.
- docs/API.md corrections (content type, alert `value`, version line).

### Verified
- Memory/threads: ~2,700 mixed requests (API stats with detection, PDFs,
  wall, dashboards) — RSS plateaus ~220–235 MB, threads and file
  descriptors constant. Browser heap, DOM nodes and listeners flat on the
  home page, a 24 h dashboard and the wall display.
- Upgrade from the previous `main` (pre-1.1) with live HTTP / TCP / Modbus
  data via `git pull` + restart under the service's gunicorn settings:
  schema migrated automatically, data kept, all pages and ingest working.

## 1.7.0 — 2026-10-24

### Added
- **Data gaps** are shown instead of hidden: lines break and the gap is
  hatched with "no data" (pauses > 5× the median spacing, ≥ 30 s) on every
  chart and in PDF charts; dashboard window edges with no data are shaded.
- **Light theme** with a ☀/☾ toggle in the menu (per-browser); chart colours
  follow the theme. **Print stylesheet** for clean light printouts.
- Home tiles: **trend arrow** (last 5 min vs previous 15, ignoring changes
  under ~10 % of the hour's range) and a brief **flash** when the value
  changes.

### Changed
- Chart grid/text colours come from CSS variables instead of hard-coded
  hex; the Query chart uses the monitor's colour.

## 1.6.0 — 2026-10-24

### Added
- **Actual show start/end detection** (`app/detect.py`). Within ±N minutes
  (default 15) of a scheduled start/end, finds the biggest sustained rise /
  drop in a dB monitor's levels (2-minute medians over 10-second buckets,
  then snapped to the exact reading); edges with a change below `min_step`
  (default 6 dB) keep the scheduled time. Every result explains itself.
- Available on the Reports page (*Find actual start/end within ±N min*),
  per show (*Actual start and end* settings; stats, PDF, CSV and callback use
  the actual range), and in the API: `detect`, `window_minutes`,
  `min_step`, `detect_monitor` on `/stats`, `/series`, `/report.pdf`;
  `auto_detect` + `detect` on shows; `detection`, `report_start`,
  `report_end` in responses and in the show-report callback.
- Charts dim everything outside the detected range and mark the scheduled
  times; report PDFs list *Scheduled* and *Detected* times.
- Shows with detection wait for the end-search window before sending their
  report.
- New `shows` columns: `auto_detect`, `detect_monitor_id`,
  `detect_window_minutes`, `detect_min_step` (added automatically).

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
