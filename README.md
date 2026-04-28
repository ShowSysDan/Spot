# Spot

**Data acquisition, down to the last spot.**

Spot is a small Flask + PostgreSQL application that ingests numeric readings and
event markers from monitors (audio processors, temperature sensors, anything
that can speak HTTP, TCP, or UDP), stores them in an isolated Postgres schema,
and lets you browse, query, graph, and export the data.

---

## Features

- **Monitor management** — add, edit, enable/disable, and delete monitors
  through the web UI. Each monitor has a name, free-form unit (`dB`, `°F`,
  `°C`, `%`, custom…), a listener type, and an auto-generated auth token.
- **Three ingest protocols per monitor:**
  - **HTTP** — `POST /api/ingest/<token>` on the shared web port
    (JSON, form-encoded, or `text/plain`).
  - **TCP** — newline-terminated lines on a per-monitor port.
  - **UDP** — datagrams on a per-monitor port.
- **Event markers** — push a label string (`"Show Start"`, `"Fire Alarm"`)
  with or without a numeric value. Events are stored alongside readings and
  rendered as vertical markers on the graph.
- **Live dashboard** — auto-refreshing chart of the last 30 s / 1 min / 5 min /
  15 min, with the latest reading and recent events.
- **Query & export** — pick a UTC time range; see min/max/avg/count + event
  count; export the range to **CSV** or a one-page **PDF** with the chart.
- **Multi-monitor overlay** — plot multiple monitors over the same time range
  on one chart. Monitors that share a unit share a Y axis; differing units get
  their own axis (left/right alternating).
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

### 6. Smoke-test (foreground)

```bash
.venv/bin/python run.py
# Open http://<host>:6100/
```

You should see the Spot home page. Stop with `Ctrl-C`.

### 7. Install as a systemd service

A templated unit is provided at `spot.service`. The template token `%i`
expands to the Linux user that should own the service (e.g. `dan`).

```bash
sudo cp ~/Spot/spot.service /etc/systemd/system/spot@.service
sudo systemctl daemon-reload
sudo systemctl enable --now spot@<user>.service
sudo systemctl status spot@<user>.service
```

The unit runs `gunicorn` with **a single worker and a thread pool**. This is
important: the TCP/UDP listeners must live in one process so they don't
double-bind ports. HTTP concurrency is provided by the threads.

If `~/Spot` isn't where you installed it, edit the unit (`WorkingDirectory`,
`EnvironmentFile`, and `ExecStart` paths) before copying it.

### 8. Open firewall ports

Open `SPOT_WEB_PORT` plus any per-monitor TCP/UDP ports you configure:

```bash
sudo ufw allow 6100/tcp        # web UI / HTTP ingest
sudo ufw allow 5140/udp        # example UDP monitor
sudo ufw allow 5141/tcp        # example TCP monitor
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
sudo systemctl restart spot@<user>.service
```

The schema is migrated by `Base.metadata.create_all`, which only adds new
tables/columns it doesn't know about — destructive changes require a manual
migration.

---

## Using Spot

### Add a monitor

Go to **New Monitor**. Pick a name, the unit, the listener type, and (for
TCP/UDP) the port. Save. The monitor's auth token appears on its detail page
along with copy-paste push examples.

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

### Live dashboard

`/monitors/<id>/dashboard` — auto-refreshing chart of the last N seconds.

### Query & export

`/monitors/<id>/query` — choose a UTC range, see summary stats, export CSV
or PDF.

Direct URLs (also usable from cron / scripts):

- `GET /data/monitor/<id>/series?start=...&end=...` — JSON points + events
- `GET /data/monitor/<id>/summary?start=...&end=...` — min/max/avg/count
- `GET /data/monitor/<id>/recent?seconds=30`         — recent points
- `GET /data/monitor/<id>/export.csv?start=...&end=...`
- `GET /data/monitor/<id>/export.pdf?start=...&end=...`
- `GET /data/overlay?ids=1,2,3&start=...&end=...`    — series for many monitors
- `GET /data/overlay/export.pdf?ids=1,2,3&start=...&end=...` — combined PDF

Timestamps are ISO-8601 in UTC (e.g. `2026-04-28T00:00:00Z`).

### Multi-monitor overlay

`/overlay` — pick monitors (Cmd/Ctrl-click to multi-select) and a UTC range.

- **In-browser**: each unit gets its own Y axis (alternating left/right) on a
  single chart.
- **PDF export**: one panel per unit stacked vertically, sharing the X axis,
  with a per-monitor min/max/avg summary table.
- **CSV export**: one CSV per selected monitor.

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
- `spot.ingest`     — malformed payloads
- `spot.janitor`    — retention purges (rows deleted per monitor per pass)
- `spot.db`         — schema bring-up
- `spot.data`       — query/export

The same records go to **stderr** (visible in `journalctl -u spot@<user>`)
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
                ├── JanitorThread (single, periodic retention enforcement)
                │
                └── SQLAlchemy (search_path = spot, public)
                              │
                              ▼
                       Postgres (shared DB, isolated schema)
```

- One process, threaded HTTP, daemon listener threads. Listener buffers are
  capped (8 KiB per line / datagram); oversize messages are logged and dropped.
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

---

## License

TBD.
