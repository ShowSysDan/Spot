# Spot

**Data acquisition, down to the last spot.**

Current version: **1.7.0** — see [CHANGELOG.md](CHANGELOG.md). The running
version is shown in the web UI footer, logged at startup, and stamped on every
PDF report.

Spot is a small Flask + PostgreSQL application that ingests numeric readings and
event markers from monitors (audio processors, temperature sensors, anything
that can speak HTTP, TCP, or UDP), stores them in an isolated Postgres schema,
and lets you browse, query, graph, and export the data.

---

## Features

- **Monitor management** — add, edit, enable/disable, and delete monitors
  through the web UI. Each monitor has a name, free-form unit (`dB`, `°F`,
  `°C`, `%`, custom…), a listener type, an auto-generated auth token, an
  optional **value-extraction regex** for sources that ship pre-formatted
  text (e.g. `47.5dB`, `7:47:32 AM — 47.5dB`), and an optional
  **decimal-places** setting that rounds each numeric reading at ingest
  time (handy for noisy float32 sources from Modbus devices).
- **Four ingest protocols per monitor:**
  - **HTTP** — `POST /api/ingest/<token>` on the shared web port
    (JSON, form-encoded, or `text/plain`).
  - **TCP** — newline-terminated lines on a per-monitor port.
  - **UDP** — datagrams on a per-monitor port.
  - **Modbus** — Spot polls a Modbus TCP device (Function Code 3, Read
    Holding Registers) on a configurable interval and decodes one or more
    values per cycle. See [Modbus devices](#modbus-devices) below.
  TCP/UDP ports are **auto-assigned** from the **6101–6199** range; Spot
  vibe-checks bindability before claiming one. The user can't pick a port.
- **Event markers** — push a label string (`"Show Start"`, `"Fire Alarm"`)
  with or without a numeric value. Events are stored alongside readings and
  rendered as vertical markers on the graph.
- **Live readout home page** — every monitor gets a tile with its latest
  value, a one-hour sparkline, the 1-hour range, and a freshness dot
  (green = reporting, amber = stale, grey = nothing recent). Tiles refresh
  every 5 s while the tab is visible and click through to the dashboard.
- **Alerts** — per-monitor *above* / *below* thresholds (judged on the
  average over a window you choose, so one loud clap doesn't page anyone) and
  a *no data for N seconds* alert. Alerting tiles glow red, dashboards draw the
  threshold lines, the menu shows a count, and every raise / recovery goes
  to syslog and optionally e-mail and a JSON webhook.
- **Shows** — named time ranges (a performance, rehearsal, event) with
  their monitors. Each gets min / max / average plus **Leq, L10 and L90** for
  dB monitors, and a PDF report that Spot can e-mail and/or POST to another
  system automatically after the show ends.
- **Actual show start/end detection** — give Spot the scheduled times and it
  finds the real curtain-up / curtain-down in the sound levels (±15 min by
  default), reports on those, and shows what it found. Falls back to the
  schedule, with a reason, when there's no clear change.
- **Scheduled reports** — daily, weekly or monthly PDF reports e-mailed
  automatically, each covering the previous complete Eastern-time period.
- **Wall display** (`/wall`) — a full-screen kiosk view with huge numbers,
  sparklines, alert colours and a 24-hour clock, for a control-room or
  backstage screen. Filter by location; bookmark one per screen.
- **External API** (`/api/v1`, API-key protected) for systems such as
  321Theater: list monitors, pull statistics, series and PDF reports for any
  time range, and create/update shows. Full guide: **[docs/API.md](docs/API.md)**.
- **US Eastern, 24-hour everywhere** — every time on screen, in charts, PDFs,
  CSVs and e-mails is shown in `SPOT_TIMEZONE` (default America/New_York,
  EST/EDT handled automatically), whatever the viewer's computer is set to.
  Data is stored in UTC.
- **Honest charts** — where a sensor stopped reporting, lines break and the
  gap is hatched ("no data") instead of drawing a straight line across it,
  on screen and in PDFs.
- **Light theme & printing** — a ☀/☾ toggle in the menu bar switches the UI to
  a light theme (remembered per browser); printing any page gives a clean,
  light layout without menus and buttons.
- **Locations & colours** — an optional location label groups tiles on the
  Monitors page (e.g. per theater); each monitor can have its own colour,
  used for its tile, chart line and PDF reports.
- **Live dashboard** — windows from 1 minute to 7 days, with current / min /
  max / average / sample-count cards, event markers drawn on the chart, a
  pause button, and an auto refresh rate that slows down for long windows.
  Long windows are downsampled on the server, so a day at 1 Hz loads ~1,500
  points instead of 86,400, with the min/max band shaded behind the line.
- **Query & export** — pick a range (or a quick preset: last hour, 24 h,
  7 days, today, yesterday, this week…); see min/max/avg/count + event count;
  export the range to **CSV** or a one-page **PDF** with the chart.
- **Reports** (formerly *Overlay*) — tick one or more monitors, pick a range,
  preview them on one chart with a per-monitor summary table, then download a
  branded **PDF report** or one combined **CSV**. Monitors that share a unit
  share a Y axis; differing units get their own axis (left/right alternating).
- **Per-monitor retention** with **global default** — each monitor can set
  its own `retention_days`; if blank, `SPOT_DEFAULT_RETENTION_DAYS` (env)
  applies. A janitor thread (default hourly) bulk-deletes expired readings;
  "Purge now" is also available per monitor.
- **Delete all data** — per-monitor button to wipe all readings for a monitor
  (the monitor itself is kept).
- **Storage report** — `/storage` shows total schema bytes, the readings
  table size, and a per-monitor row count + estimated size.
- **Syslog integration** — application errors, listener start/stop, retention
  purges, and malformed payloads are sent to syslog (configurable facility).
- **Isolated schema** — Spot uses a single Postgres schema (default `spot`)
  inside a shared database; nothing leaks into `public`.

> No authentication: the web UI and API are currently open. Front Spot with a
> reverse proxy (nginx, Caddy, Traefik) and your existing auth layer until the
> integration described in the project goals is wired in.

---

## Quick install (Debian, user-folder install, runs as a systemd service)

The instructions below assume:

- Debian 12+ (similar steps work on Ubuntu).
- The app lives in `/home/<user>/Spot` (clone or copy this directory there).
- A Postgres instance is reachable from this host.
- The Linux user that will run Spot is referred to as `<user>` below.

### 1. System packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip \
                    libpq-dev build-essential rsyslog
```

`libpq-dev` is required to build `psycopg2`. `rsyslog` is the default on
Debian; if you use `syslog-ng`, that works too — just point `SPOT_SYSLOG_*` at
its socket.

### 2. Get the code

```bash
cd ~
git clone <your-fork-or-tarball-url> Spot
cd Spot
```

(or copy the project directory into `~/Spot`)

### 3. Python virtualenv & dependencies

```bash
cd ~/Spot
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

**No internet on the server?** Spot runs fully offline: every browser
library (Chart.js and its date adapter) is vendored in `app/static/vendor/`
and served by Spot itself, so the UI never touches a CDN. For the Python
packages, build a wheelhouse on a machine that does have internet and the
same OS / architecture / Python version, then copy it across:

```bash
# on the connected machine, inside the Spot directory
./scripts/build-wheelhouse.sh            # creates ./wheelhouse/

# on the offline server, after copying the Spot directory over
python3 -m venv .venv
.venv/bin/pip install --no-index --find-links wheelhouse -r requirements.txt
```

### 4. Postgres database

Spot uses a **shared database with an isolated schema** (default schema name:
`spot`). You only need a database and a role; Spot will create the schema and
tables on first start.

```sql
-- run as a superuser, e.g. `sudo -u postgres psql`
CREATE ROLE spot_user WITH LOGIN PASSWORD 'choose-a-strong-password';
CREATE DATABASE shared OWNER spot_user;   -- or use an existing shared DB
GRANT CREATE, USAGE ON DATABASE shared TO spot_user;
```

If the database already exists and is owned by someone else, grant the role the
ability to create the schema:

```sql
GRANT CREATE ON DATABASE shared TO spot_user;
```

### 5. Configure

```bash
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Set at least:

| Variable              | What to put                                  |
|-----------------------|----------------------------------------------|
| `SPOT_DB_HOST`        | DB host / IP                                 |
| `SPOT_DB_PORT`        | DB port (default `5432`)                     |
| `SPOT_DB_NAME`        | Shared DB name                               |
| `SPOT_DB_USER`        | Spot's role                                  |
| `SPOT_DB_PASSWORD`    | Spot role password                           |
| `SPOT_DB_SCHEMA`      | `spot` (or any name; will be created)        |
| `SPOT_WEB_HOST`       | `0.0.0.0` (bind everywhere) or a specific IP |
| `SPOT_WEB_PORT`       | e.g. `6100`                                  |
| `SPOT_SYSLOG_ADDRESS` | `/dev/log` for local rsyslog                 |
| `SPOT_SYSLOG_FACILITY`| e.g. `local0`                                |
| `SPOT_LOG_LEVEL`      | `INFO` / `DEBUG` / `WARNING`                 |
| `SPOT_SECRET_KEY`     | Any random string                            |
| `SPOT_INGEST_ALLOW`   | Optional CSV of allowed source IPs           |
| `SPOT_JANITOR_INTERVAL_SECONDS` | Retention-cleanup interval (default `3600`) |
| `SPOT_DEFAULT_RETENTION_DAYS` | Global default retention; blank = keep forever |
| `SPOT_TIMEZONE`       | Display zone for all times, 24-hour (default `America/New_York`) |
| `SPOT_PUBLIC_URL`     | Optional. Base URL used for links in e-mails / webhooks, e.g. `http://spot.internal:6100` |
| `SPOT_SMTP_HOST`      | Optional. Mail server for alert / report e-mails; blank = e-mail off |
| `SPOT_SMTP_PORT`      | Default `587` |
| `SPOT_SMTP_SECURITY`  | `starttls` (default), `ssl`, or `none` |
| `SPOT_SMTP_USER` / `SPOT_SMTP_PASSWORD` | Optional SMTP login |
| `SPOT_SMTP_FROM`      | Sender address, default `spot@localhost` |
| `SPOT_ALERT_EMAIL_TO` | Comma-separated alert recipients |
| `SPOT_ALERT_WEBHOOK_URL` | Optional URL that receives alert JSON |
| `SPOT_ALERT_INTERVAL_SECONDS` | How often alerts are evaluated (default `15`, min `5`) |

### 6. Smoke-test (foreground)

```bash
.venv/bin/python run.py
# Open http://<host>:6100/
```

You should see the Spot home page. Stop with `Ctrl-C`.

### 7. Install as a systemd service

A regular (non-templated) `spot.service` is provided. The bundled installer
fills in the user and install path, drops the unit at
`/etc/systemd/system/spot.service`, and enables it:

```bash
cd ~/Spot
sudo ./scripts/install-service.sh
```

By default the script uses the user that invoked `sudo` (so if you
`sudo ./scripts/install-service.sh` as `cuelight`, the service runs as
`cuelight`) and the current Spot directory. Override either:

```bash
sudo ./scripts/install-service.sh dan /home/dan/Spot
# or
SPOT_USER=dan SPOT_HOME=/home/dan/Spot sudo -E ./scripts/install-service.sh
```

After install:

```bash
sudo systemctl status spot
sudo systemctl restart spot
sudo journalctl -u spot -f
```

The unit runs `gunicorn` with **a single worker and a thread pool**. This is
important: the TCP/UDP listeners must live in one process so they don't
double-bind ports. HTTP concurrency is provided by the threads.

To uninstall:

```bash
sudo systemctl disable --now spot
sudo rm /etc/systemd/system/spot.service
sudo systemctl daemon-reload
```

### 8. Open firewall ports

Open `SPOT_WEB_PORT` plus the TCP/UDP monitor port range (6101–6199):

```bash
sudo ufw allow 6100/tcp           # web UI / HTTP ingest
sudo ufw allow 6101:6199/tcp      # TCP monitors (auto-assigned in this range)
sudo ufw allow 6101:6199/udp      # UDP monitors (auto-assigned in this range)
```

### 9. Syslog

If `SPOT_SYSLOG_ADDRESS=/dev/log`, messages reach the local syslog daemon.
To route Spot's `local0` messages to a dedicated file, drop a file at
`/etc/rsyslog.d/30-spot.conf`:

```
if $programname == 'spot' then /var/log/spot.log
& stop
```

then `sudo systemctl restart rsyslog`.

For remote syslog, set `SPOT_SYSLOG_ADDRESS=loghost.example.com:514`.

### 10. Upgrades

```bash
cd ~/Spot
git pull        # or copy in the new tree
.venv/bin/pip install -r requirements.txt
sudo systemctl restart spot
```

Check the footer (or `journalctl -u spot | grep started`) to confirm the new
version is running.

The schema is migrated by `Base.metadata.create_all`, which only adds new
tables/columns it doesn't know about — destructive changes require a manual
migration.

---

## Using Spot

### Add a monitor

Go to **New Monitor**. Pick a name, the unit, and the listener type. For
TCP/UDP, Spot picks an available port in 6101–6199 for you on save (it
actually binds the port to confirm it's free; if not it tries the next).
The monitor's auth token and assigned port appear on its detail page, along
with copy-paste push examples.

Open firewall rules for whichever ports get assigned, e.g.:

```bash
sudo ufw allow 6100/tcp           # web UI / HTTP ingest
sudo ufw allow 6101:6199/tcp      # TCP monitors
sudo ufw allow 6101:6199/udp      # UDP monitors
```

### Value-extraction regex

Some sources push numbers wrapped in formatting (e.g. `7:47:32 AM — 47.5dB`).
On the monitor's edit form, set **Value extraction regex** to pull the number
out. The regex is `re.search`-applied to each line/payload, then:

1. If a named group `(?P<value>…)` matches, that's the numeric value.
2. Otherwise the first capturing group is used.
3. Otherwise the whole match is used.
4. An optional named group `(?P<label>…)` becomes the annotation.

Useful patterns:

| Incoming                   | Pattern                                    |
|----------------------------|--------------------------------------------|
| `47.5dB`                   | `(\d+(?:\.\d+)?)\s*dB`                     |
| `7:47:32 AM — 47.5dB`      | `(?P<value>-?\d+(?:\.\d+)?)\s*dB`          |
| `temp=72.4F`               | `temp=(?P<value>-?\d+(?:\.\d+)?)`          |
| Just any first number      | `(?P<value>-?\d+(?:\.\d+)?)`               |

Leave blank to use the default free-form parser
(`92.3`, `92.3,Show Start`, `Fire Alarm`, JSON, etc.).
A bad regex match goes to syslog as `malformed data … err=regex did not match …`.

### Push data

#### HTTP

```bash
# Numeric reading
curl -X POST -H "Content-Type: application/json" \
  -d '{"value": 92.3}' \
  http://HOST:6100/api/ingest/<TOKEN>

# Reading + label (annotation on a single sample)
curl -X POST -H "Content-Type: application/json" \
  -d '{"value": 92.3, "label": "Show Start"}' \
  http://HOST:6100/api/ingest/<TOKEN>

# Plain text body works too
curl -X POST -H "Content-Type: text/plain" --data '92.3' \
  http://HOST:6100/api/ingest/<TOKEN>

# Event marker only (no numeric value)
curl -X POST -H "Content-Type: text/plain" --data 'Fire Alarm' \
  http://HOST:6100/api/event/<TOKEN>
```

JSON payload shape:

```json
{ "value": 92.3, "label": "Show Start", "ts": "2026-04-28T01:00:00Z" }
```

`ts` is optional; it is used for back-dating. If omitted, server time is used.

#### TCP

Newline-terminated lines:

```
92.3
92.5,Show Start
Fire Alarm
```

Each line is one reading or event. `value`, `value,label`, or just `label`
are all accepted (comma, space, or tab separates value from label).

```bash
printf '92.3\n92.5,Show Start\n' | nc HOST <PORT>
```

#### UDP

One reading per datagram (or newline-separated lines per datagram):

```bash
printf '92.3' | nc -u -w1 HOST <PORT>
```

### Monitors (home page)

`/` shows a live tile per monitor, grouped under headings by **Location**
when any monitor has one, plus a **Manage** table (collapsible; the
open/closed state and the filter box are remembered per browser).

- The big number is the latest value, rounded to the monitor's
  *decimal places* when set.
- The sparkline covers the last hour (60 one-minute averages); the
  bottom-right figure is the low–high of those averages.
- A small arrow next to the value shows the direction of travel — the last
  5 minutes against the 15 before (↑ / ↓ with the change, → when steady);
  the value flashes briefly in the monitor's colour when it changes.
- The dot is green while the monitor is reporting, amber once its newest
  reading is older than the stale threshold, and grey when it is long
  overdue or has no data. The threshold is 5 minutes, or 3× the poll
  interval for Modbus monitors (minimum 30 s).
- Polling pauses while the browser tab is hidden.

### Gaps, themes and printing

- **Gaps:** a pause longer than 5× the sensor's usual spacing (and at least
  30 s) is a gap. Chart lines break there and the stretch is hatched with a
  "no data" label — dashboards, Query, Reports, show pages and PDFs alike.
  On a dashboard, "nothing yet" at the start of the window and "nothing
  lately" at the end are shaded too.
- **Theme:** the ☀/☾ button at the right of the menu switches between the
  dark and light themes; the choice is stored in that browser. The wall
  display always stays dark. PDF reports keep Spot's dark branded style.
- **Printing:** browser print uses a light, chrome-free layout (no menus,
  filters or buttons; charts and tables kept together).

### Time & timezone

Spot stores every timestamp in UTC and shows every time — pages, charts,
date pickers, PDFs, CSV files, e-mails, API responses — in **one display
zone, 24-hour**: `SPOT_TIMEZONE`, default `America/New_York` (EST/EDT switch
automatically). It does not depend on the viewer's computer or on the
server's own timezone setting, so a server reinstalled with a UTC clock
can't silently shift reports. Setup → *Time* shows the display zone next to
the server's own and warns if they differ (harmless, but logs from other
tools will use the server's zone).

Date/time fields are a date picker plus a 24-hour `HH:MM:SS` box. Quick
ranges like *Today* and *This week* start at Eastern midnight. CSV
timestamps carry their offset (`2026-10-24T19:30:00-04:00`); download
file names use Eastern time.

### Shows

`/shows` lists upcoming, live and past shows. A show is a name, a start and
end, and the monitors that matter for it — e.g. *Hamilton — Sat 7:30 PM* with
the Walt Disney Theater's sound meters.

- **Create** with *+ New show*, from Reports (*Save as show…* takes the
  selected monitors and range), or from another system via the API.
- **Show page** — when, delivery status, a levels table (samples, min, max,
  average, and for dB monitors **Leq** — the energy-average level, the
  standard "average loudness" — plus **L10** (loud passages) and **L90**
  (background)), a chart, and *Download PDF* / *CSV* / *Open in Reports*.
- **Automatic report** — give a show e-mail recipients and/or a callback URL
  and Spot sends its PDF about 5 minutes after it ends (so late readings are
  included), retrying with backoff (1, 2, 4, 8, 16 min; 6 attempts) if
  delivery fails. *Send now* / *Send again* on the show page sends
  immediately. Editing a show re-arms delivery.
- Reports has a **Load a show…** menu that fills in a show's monitors and
  range.

### Detecting the actual start and end

Published times are approximate — a 21:00 curtain might go up at 20:54 or
21:04. Tick **Find actual start/end** on the Reports page (or *Actual start
and end* on a show, or send `detect=1` / `"auto_detect": true` through the
API) and Spot looks at a dB monitor's levels within ±15 minutes (adjustable)
of each scheduled edge:

- **start** = the biggest sustained **rise** near the scheduled start,
- **end** = the biggest sustained **drop** near the scheduled end,

comparing 2-minute medians either side so a single clap or shout can't
trigger it, then snapping to the exact reading. A change smaller than 6 dB
(adjustable) isn't trusted and that edge keeps the scheduled time.

Statistics, the PDF and CSV then cover the actual range; the chart dims
everything outside it and marks the scheduled times; and a line explains the
result, e.g. *Scheduled 21:00–23:30 · actual 21:04–23:40: start +4 min
(rise of 22.1 dB); end +11 min (drop of 28.4 dB)*. One reference monitor
(the first dB monitor, or the one you pick) decides the times for all
monitors in the report. Shows with detection on wait an extra 17 minutes
before e-mailing / sending their report so a late finish is caught. Full
details: [docs/API.md §5.1](docs/API.md#51-finding-the-actual-start-and-end-detect).

### Scheduled reports

**Setup → Scheduled reports** sends a PDF report by e-mail on a schedule
(needs `SPOT_SMTP_HOST`). Each report covers the previous **complete**
period in Eastern time, never a partial one:

| Frequency | Sent | Covers |
|-----------|------|--------|
| Daily | every day at HH:MM | yesterday 00:00 – 24:00 |
| Weekly | the chosen weekday at HH:MM | the 7 days before that day (Monday → last Mon–Sun) |
| Monthly | the 1st at HH:MM | the previous calendar month |

The e-mail body lists min / max / average (and Leq for dB monitors) per
monitor; the PDF is attached. *Send now* e-mails the most recent complete
period without changing the schedule. The next send time is stored, so a
restart neither skips nor repeats a report; if Spot was down at send time,
the missed report goes out once when it's back (for the period it was due
for). Daylight-saving days are handled (the fall-back day is 25 hours).

### Wall display

`/wall` (or *Wall display* on the Monitors page) is a full-screen view for a
TV or monitor:

- One big tile per enabled monitor: name, value sized to fill the tile,
  unit, a 30-minute sparkline, "N ago", and the monitor's colour.
- Alerting tiles turn red with a `HIGH` / `LOW` / `STALE` tag (amber once
  acknowledged); readings older than 5 minutes are greyed.
- A large 24-hour Eastern clock. Values refresh every 3 seconds; the value
  flashes in the monitor's colour when it changes.
- `?location=Walt%20Disney%20Theater` limits it to one location (the menu in
  the header does the same); `?ids=1,2` picks monitors. Bookmark the URL
  per screen.
- **F** or the ⛶ button toggles full screen; the mouse cursor hides after 3
  seconds of no movement; the browser is asked to keep the screen awake
  (works over HTTPS; on plain HTTP set the display's own sleep timer off).

### External API (321Theater and other systems)

Other applications reach Spot over HTTP at `/api/v1`, authenticated with API
keys created under **Setup → API keys** (shown once; stored hashed). They
can list monitors with latest values, get statistics (including Leq / L10 /
L90), downsampled series and PDF reports for any monitors and time range,
and create, update, delete and fetch shows by their own `external_id`.
Spot can push each show's report to a callback URL when it ends.

**Everything an integrator needs — endpoints, payloads, errors, callback
format, retry rules and code samples — is in [docs/API.md](docs/API.md).**

### Alerts & notifications

Set thresholds on a monitor's **Edit** page, in the *Alerts* box:

| Field | Meaning |
|-------|---------|
| Alert above / Alert below | Raise a `HIGH` / `LOW` alert when the value crosses it. Blank = off. |
| Averaged over (seconds) | The value judged is the average of the last N seconds (0 or blank = the latest reading). 30–60 s works well for sound levels. |
| Alert when no data for (seconds) | Raise a `STALE` alert when no reading has arrived for this long (min 10). |

The alert engine runs every `SPOT_ALERT_INTERVAL_SECONDS` (default 15).
It waits about 30 s after Spot starts so a restart doesn't flag everything as
stale. Each crossing opens **one** alert, which tracks the worst value seen
and closes when the monitor is back in range (a `HIGH`/`LOW` alert only
clears on a real reading; missing data is the `STALE` alert's job).
Disabling a monitor or clearing its thresholds closes its open alerts.

Where alerts show up:

- **Monitors page** — the tile glows red with a `HIGH` / `LOW` / `STALE`
  tag (amber once acknowledged).
- **Dashboard** — a banner above the chart and dashed threshold lines on it.
- **Alerts page** (`/alerts`) — active alerts with **Acknowledge**, every
  monitor that has thresholds, and the last 200 past alerts. The menu badge
  counts active, unacknowledged alerts.
- **syslog** — `spot.alerts` logs every raise (WARNING) and recovery (INFO).
- **E-mail** — when `SPOT_SMTP_HOST` and `SPOT_ALERT_EMAIL_TO` are set.
- **Webhook** — when `SPOT_ALERT_WEBHOOK_URL` is set, Spot POSTs:

```json
{
  "event": "alert.raised",            // or "alert.cleared"
  "alert_id": 12,
  "kind": "high",                     // high | low | stale
  "message": "101.8 dB (10s average) is above the 95 dB limit",
  "monitor": {"id": 1, "name": "Stage Left dB", "unit": "dB", "location": "Walt Disney Theater"},
  "value": 101.79,
  "threshold": 95.0,
  "started_at": "2026-10-24T17:39:24-04:00",
  "cleared_at": null,
  "url": "http://spot.internal:6100/monitors/1/dashboard"
}
```

**Setup → Notifications** shows the current mail / webhook settings and has
*Send test e-mail* and *Send test webhook* buttons.

### Live dashboard

`/monitors/<id>/dashboard` — auto-refreshing chart of the last N seconds.

- **Windows:** 1 m, 5 m, 15 m, 1 h, 3 h, 6 h, 12 h, 24 h, 48 h, 7 d. The
  choice is remembered per monitor and kept in the URL
  (`?window=86400`), so a link opens the same view.
- **Refresh:** *Auto* refreshes every 1 s for short windows and backs off to
  once a minute for multi-day ones; a fixed rate can be picked instead.
  **Pause** freezes the chart.
- **Stats cards:** current value, plus min / max (with when they happened),
  average and sample count for the whole window. These are computed in SQL
  from every reading, even when the chart itself is downsampled.
- **Downsampling:** when a window holds more than 1,500 readings, the chart
  shows fixed-width bucket averages with the min/max band shaded. A note
  under the sample count says how wide the buckets are.
- **Events** appear as dashed red markers on the chart and in the list below.
- **Report this window** opens Reports with this monitor and the visible
  range already filled in.

### Query & export

`/monitors/<id>/query` — choose a UTC range, see summary stats, export CSV
or PDF.

Direct URLs (also usable from cron / scripts):

- `GET /data/monitor/<id>/series?start=...&end=...` — JSON points + events
- `GET /data/monitor/<id>/summary?start=...&end=...` — min/max/avg/count
- `GET /data/monitor/<id>/recent?seconds=30`         — recent points (add `&stats=1` for window min/max/avg/count)
- `GET /data/monitor/<id>/latest`                    — only the most recent reading (`{ts, value, label}`)
- `GET /data/latest`                                 — latest reading + 1 h sparkline for every monitor (`?ids=1,2` to narrow, `?spark_minutes=N` up to 1440)
- `GET /data/monitor/<id>/export.csv?start=...&end=...`
- `GET /data/monitor/<id>/export.pdf?start=...&end=...`
- `GET /data/overlay?ids=1,2,3&start=...&end=...`    — series for many monitors (add `&summary=1` for exact per-monitor stats)
- `GET /data/overlay/export.pdf?ids=1,2,3&start=...&end=...` — combined PDF report
- `GET /data/overlay/export.csv?ids=1,2,3&start=...&end=...` — combined CSV (`timestamp,monitor,unit,value,label`)

Common parameters:

| Parameter | Applies to | Meaning |
|-----------|-----------|---------|
| `start`, `end` | range endpoints | ISO-8601 timestamps (see below). `end` defaults to now. |
| `minutes` | range endpoints | Used when `start` is omitted: range is the last N minutes (default 60). |
| `max_points` | `series`, `recent`, `overlay` | If the range holds more readings than this (10–10,000), return fixed-width bucket averages instead. Bucketed points carry `min`/`max` too, and the response's `bucket_seconds` says how wide each bucket is (`null` = raw readings). |

Limits: a single request can cover at most **366 days**; `overlay` accepts at
most 50 ids; series responses include at most the 500 most recent event
markers. Malformed timestamps or integers return **400** with a message
(they used to return 500). CSV exports are streamed straight from the
database, so a long export uses the same memory as a short one; PDF charts are
drawn from at most 4,000 bucketed points, while their stats always use every
reading.

Timestamps in URLs accept ISO-8601 with offset (`2026-04-28T15:00:00-04:00`)
or with `Z` for UTC (`2026-04-28T19:00:00Z`). The web UI's date pickers and
all rendered output (storage page, PDF axes, CSV files) use the **server's
local timezone**, set via `timedatectl set-timezone America/New_York`
or by `Environment=TZ=America/New_York` in the systemd unit. Internally,
Spot stores everything as UTC and converts only at the display layer.

### Modbus devices

Modbus TCP sensors (temperature/humidity probes, PLCs, etc.) are passive —
they only reply when polled. Spot's Modbus support introduces a small
**Modbus device** entity that holds the connection and polling settings, and
each *Monitor* can be linked to a device with a register address and a data
type. One TCP poll per cycle reads the configured register block, then
decodes one value for each linked Monitor.

Why a separate entity? Most Modbus sensors expose multiple values from one
device — e.g. a Monigear sensor returns temperature and humidity together.
Each value wants its own unit, retention, dashboard, and overlay treatment,
which means each is its own Monitor. The device record is the shared
"connection" the Monitors hang off of.

#### Add a device

Go to the **Modbus** tab → **New Modbus device** and fill in:

| Field              | Default | Notes |
|--------------------|---------|-------|
| Host               | —       | IP or DNS of the device (port 502 by default). |
| Slave / unit ID    | `0`     | Most simple devices use 0. |
| Poll interval      | `10`    | Seconds between cycles. Minimum 1. |
| Read timeout       | `5`     | Seconds before a single read is given up on. |
| Word order         | `CDAB`  | Multi-register endianness. Monigear is `CDAB`; others may be `ABCD` / `BADC` / `DCBA`. If decoded values are nonsense, try a different order. |
| Register start     | `0`     | First holding register to read each cycle. |
| Register count     | `4`     | How many 16-bit registers to read per cycle (Modbus spec ceiling: 125). |
| Auto-create temp/humidity | on  | One-click: also creates two Monitors — `<name> Temperature` (°F, register `start`, `float32`) and `<name> Humidity` (%, register `start+2`, `float32`). |

Saving with **Auto-create** ticked is the one-form path for the common
Monigear-style temp+humidity sensor. The two auto-created Monitors default
to **1 decimal place** of rounding so the dashboard shows `48.9 %` instead
of `48.941001892089844 %`. Untick auto-create to add Monitors manually
afterwards from the device's detail page.

#### Add a Monitor that reads a Modbus value

On the monitor form, set **Listener type** to `MODBUS`. New fields appear:

- **Modbus device** — pick a device from the dropdown. Manage devices on the
  Modbus tab.
- **Register address** — absolute holding-register address. Must fall inside
  the device's `start … start+count-1` window.
- **Data type** — `float32` (default), `uint16`, `int16`, `uint32`, `int32`.
  32-bit types consume two registers (subject to word order); 16-bit types
  consume one.

The Modbus device's poll interval is what governs how fast readings arrive;
the Monitor's regex / port / token fields don't apply. The Monitor still
has its own unit, retention, and dashboard, just like any other.

#### Cycle behavior

For each enabled device, one daemon thread runs a loop:

1. Open a TCP connection to `host:port`.
2. Send one Read Holding Registers (FC3) request for `register_count`
   registers starting at `register_start`.
3. Read the response, parse the registers.
4. For every enabled Monitor linked to this device:
   - compute the register slice from `address - register_start`,
   - decode it per `data_type` and the device's `word_order`,
   - store one Reading on that Monitor.
5. Disconnect, sleep `poll_interval_seconds`, repeat.

Failures (timeout, refused connection, Modbus exception, decode mismatch)
are logged to syslog under `spot.modbus`. The cycle continues to the next
poll without writing readings; affected Monitors will simply show a gap on
their dashboards.

#### Quick example: a Monigear sensor at `10.201.3.202`

1. Modbus → New Modbus device:
   - Name: `MDF Sensor`, Host: `10.201.3.202`,
   - Defaults for the rest (port 502, slave 0, CDAB, regs 0–3),
   - **Auto-create temp/humidity** ticked.
2. Save. Two Monitors appear: `MDF Sensor Temperature` (°F) and
   `MDF Sensor Humidity` (%), each with its own dashboard.
3. The poller starts immediately if the device is enabled. **Poll now** on
   the device page does a one-shot read so you can confirm the wiring.

### Reports

`/reports` (the old `/overlay` URL redirects here) is where multi-monitor
charts and the PDF / CSV reports live.

1. Tick monitors in the left panel. They are grouped by unit; the filter box,
   **All** and **None** help with long lists.
2. Pick a range: type it, or click a preset (last hour, 6 h, 24 h, 7 days,
   30 days, today, yesterday, this week). Times are browser-local.
3. **Preview** draws every selected monitor on one chart, one Y axis per
   unit, with a summary table (samples, min, max, average, events).
4. **Download PDF** — branded landscape report: one chart panel per unit
   stacked on a shared time axis, plus the per-monitor summary table.
   **Download CSV** — one combined file with a `monitor` and `unit` column.

The page URL updates as you preview (`/reports?ids=1,2&start=…&end=…`), so a
report can be bookmarked or shared. Each monitor's dashboard, detail page and
Manage row has a **Report** link that opens this page pre-filled.

### Retention & storage

Each monitor has a `Retention (days)` field on its edit form. Leave blank to
fall back to the global default `SPOT_DEFAULT_RETENTION_DAYS`; if that is
also blank, readings are kept forever. The janitor thread runs every
`SPOT_JANITOR_INTERVAL_SECONDS` (default 3600) and deletes expired readings;
each pass logs `purged N rows monitor=… retention=…d` to syslog.

The monitor detail page also has a **Delete all data** button that wipes all
readings for that monitor (the monitor itself stays). It logs a warning to
syslog with the row count deleted.

`/storage` shows:

- total bytes used by the `spot` schema (all tables + indexes + toast)
- bytes used by the `readings` table specifically
- per-monitor row count, oldest/newest sample, and an *estimated* size
  (proportional split of the readings table by row count — accurate to within
  a few percent unless a monitor's labels are unusually long)

Per-monitor "Purge now" buttons on the detail page run an immediate cleanup
without waiting for the janitor.

---

## Logging / syslog details

All Spot loggers are children of `spot.*`:

- `spot.web`        — UI actions (monitor created/updated/deleted, toggled, purged)
- `spot.api`        — HTTP ingest + allow-list rejections
- `spot.listeners`  — TCP/UDP listener start/stop/bind errors
- `spot.modbus`     — Modbus poller start/stop, read failures, decode errors
- `spot.ingest`     — malformed payloads
- `spot.janitor`    — retention purges (rows deleted per monitor per pass)
- `spot.db`         — schema bring-up
- `spot.data`       — query/export

The same records go to **stderr** (visible in `journalctl -u spot`)
**and** syslog (the facility you set in `SPOT_SYSLOG_FACILITY`).

---

## Architecture notes

```
Browser ──► Flask (web UI, /api/ingest, /api/event, /data/...)
                │
                ├── ListenerManager
                │     ├── TCPListener thread (per enabled TCP monitor)
                │     └── UDPListener thread (per enabled UDP monitor)
                │
                ├── ModbusManager
                │     └── ModbusPoller thread (per enabled Modbus device)
                │           ↳ Reads FC3, dispatches to bound Monitors
                │
                ├── JanitorThread (single, periodic retention enforcement)
                │
                └── SQLAlchemy (search_path = spot, public)
                              │
                              ▼
                       Postgres (shared DB, isolated schema)
```

- One process, threaded HTTP, daemon listener threads. Listener buffers are
  capped (8 KiB per line / datagram); oversize messages are logged and dropped.
  Each TCP listener accepts at most 32 simultaneous connections (extra ones
  are closed and logged) and closes its open connections when stopped.
- The DB engine uses a small connection pool with `pool_pre_ping`; on shutdown
  the engine is disposed.
- Charts are rendered server-side with the matplotlib OO API (no `pyplot`
  global state) so PDF export is safe under threaded gunicorn workers.
- Run with **a single gunicorn worker** so listener ports don't collide.

---

## Development

```bash
.venv/bin/python run.py
```

Schema is created on first start. To wipe the data and start over:

```sql
DROP SCHEMA spot CASCADE;
CREATE SCHEMA spot AUTHORIZATION spot_user;
```

### Front-end layout

- `app/static/spot.js` — shared helpers exposed as `window.Spot`: value
  formatting, "N ago" text, quick-range presets, a non-overlapping poller
  that pauses in hidden tabs, the sparkline renderer, the Chart.js
  event-marker plugin, `data-confirm` form confirmation, and `data-copy`
  copy buttons.
- `app/static/style.css` — one dark theme; colours are CSS variables on
  `:root` and mirrored in `app/exports.py` for PDFs.
- Static URLs carry `?v=<version>` so browsers fetch fresh CSS/JS after an
  upgrade.

### Code layout notes

- `app/workers.py` — `WorkerRegistry`, the one-thread-per-id bookkeeping
  used by both the TCP/UDP `ListenerManager` and the `ModbusManager`.
- `app/db.py` — to add a column to an existing table, append a line to
  `_ADDED_COLUMNS`; it is applied idempotently on every start.
- `app/routes/web.py` — form handlers validate with `_form_str` /
  `_form_int` / `_check`, which raise `_FormError`; the wrapper flashes the
  message and sends the user back to the form.
- `app/exports.py` — `_build_report()` lays out both PDF reports.

### Versioning

The version lives in `app/version.py` and is bumped with every change;
`CHANGELOG.md` records what changed in each version.

---

## License

TBD.
