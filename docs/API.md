# Spot integration guide (API v1)

This is the contract for systems that talk to Spot — primarily
**321Theater**, but nothing here is 321Theater-specific. It covers:

1. [How the two apps fit together](#1-how-the-two-apps-fit-together)
2. [Authentication](#2-authentication)
3. [Conventions](#3-conventions) — base URL, time, errors, limits
4. [Endpoint reference](#4-endpoint-reference)
5. [Statistics: what each number means](#5-statistics-what-each-number-means)
   — and [finding the actual show start/end](#51-finding-the-actual-start-and-end-detect)
6. [What Spot sends to you](#6-what-spot-sends-to-you) — show-report callback, alert webhook
7. [Pushing data into Spot](#7-pushing-data-into-spot) — ingest endpoints
8. [Recipes for 321Theater](#8-recipes-for-321theater)
9. [Code examples](#9-code-examples)
10. [Security & operations](#10-security--operations)
11. [Versioning policy](#11-versioning-policy)

Describes API v1 as of Spot **1.7.1**. `GET /` reports the running version.

---

## 1. How the two apps fit together

Spot and 321Theater are **separate applications** that communicate **only
over HTTP**. Even if they share a Postgres server, neither reads or writes the
other's schema.

| | Owns | Asks the other for |
|---|---|---|
| **321Theater** | venues, performances, schedules, which sensors belong to which venue | sensor statistics and PDF reports for a time range |
| **Spot** | sensors ("monitors"), their readings, alerts, report rendering | nothing — it only answers, and optionally calls back |

The key idea: **321Theater decides *what* to ask for** (which monitor ids,
which time range), **Spot answers**. Spot has no idea what a "venue" is in
321Theater's terms — 321Theater keeps its own venue → monitor-id mapping
(see [recipe 8.1](#81-map-your-venues-to-spot-monitors)).

There are two ways to get numbers out of Spot:

* **Ad hoc** — `GET /api/v1/stats` (and `/report.pdf`) for any monitors and
  any time range. Nothing is stored in Spot. Best for "give me the levels for
  this performance" after the fact.
* **Shows** — 321Theater registers a performance with Spot
  (`POST /api/v1/shows`, keyed by your own `external_id`). Spot then lists it
  in its UI, and **after the show ends it can push the report back to you**
  (a callback URL) and/or e-mail it. Best when you want the PDF delivered
  automatically.

You can use either or both.

---

## 2. Authentication

Every `/api/v1` request needs an **API key**.

**Creating a key:** in Spot, go to **Setup → API keys**, enter a name (e.g.
`321Theater production`) and click *Create key*. The key — `spot_` followed
by 43 random characters — is shown **once**. Spot stores only its SHA-256
hash, so a lost key can't be recovered; delete it and create a new one.

**Sending it** — either header works:

```http
Authorization: Bearer spot_9iKIans3…
```
```http
X-API-Key: spot_9iKIans3…
```

**Failures** return `401`:

```json
{"error": "API key required (Authorization: Bearer <key> or X-API-Key)", "status": 401}
{"error": "invalid or disabled API key", "status": 401}
```

**Rotation:** create a second key, switch 321Theater over, then disable or
delete the old one. Keys can be disabled (reversible) or deleted
(permanent) at any time; the change takes effect on the next request. The
*Last used* column (updated at most every 5 minutes) shows whether a key is
still in use.

> Keys protect `/api/v1` only. The Spot web UI and its internal `/data`
> endpoints are not authenticated — see [section 10](#10-security--operations).

---

## 3. Conventions

### Base URL

```
http://<spot-host>:<port>/api/v1
```

e.g. `http://spot.internal:6100/api/v1`. All paths below are relative to it.

### Time

* **Spot answers in US Eastern time with an explicit offset**, e.g.
  `2026-10-24T19:30:00-04:00` (EDT) or `2026-12-24T19:30:00-05:00` (EST).
  The offset makes every timestamp unambiguous — parse it as an
  offset-aware datetime and you have the exact instant. (The zone is Spot's
  `SPOT_TIMEZONE` setting, default `America/New_York`; `GET /` reports it.)
* **Spot accepts** any ISO-8601 timestamp:
  * with an offset: `2026-10-24T19:30:00-04:00` ← **recommended**
  * UTC: `2026-10-24T23:30:00Z`
  * **without an offset it is taken as UTC**, not Eastern. Always send an
    offset to avoid a 4–5 hour surprise.
* Seconds and fractional seconds are optional on input.
* Ranges are **inclusive** at both ends: `start <= reading time <= end`.
* Storage is UTC internally; daylight-saving changes never create gaps or
  duplicates in the data.

### Range parameters

Endpoints that take a time range accept:

| Parameter | Meaning |
|-----------|---------|
| `start` | Range start (ISO-8601). If omitted, `minutes` is used. |
| `end` | Range end (ISO-8601). Default: now. |
| `minutes` | Only when `start` is omitted: the range is the last *N* minutes before `end` (default 60). |

`start` must be before `end`; a range can be at most **366 days**.

### Detection parameters

`/stats`, `/series` and `/report.pdf` can report on the **actual** show
start and end found in the sound levels instead of the times you sent —
see [5.1](#51-finding-the-actual-start-and-end-detect) for how it works.

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `detect` | off | `1` / `true` turns detection on. |
| `window_minutes` | `15` | How far either side of `start` and of `end` to look (1–60). |
| `min_step` | `6` | Smallest sustained level change, in dB, that counts (0.5–60). |
| `detect_monitor` | first dB monitor in `monitors` (in the order you list them) | Which monitor's levels decide the times. It may be any monitor, even one not in `monitors` (e.g. a stage mic deciding the times for a report on the house meters); an unknown id is a `404`. The detected range is then applied to every monitor in the request. |

### Monitor ids

Monitors (sensors) are identified by Spot's integer **`id`**. Ids never
change or get reused while a monitor exists; renaming a monitor keeps its id.
Get the list from `GET /monitors`. Endpoints that take several use
`monitors=1,2,3` (comma-separated; `ids=` is accepted as an alias), at most
50 per request — more is a `400`, never silently trimmed.

### Responses and errors

* JSON responses are `application/json` (UTF-8).
* Numbers are plain JSON numbers (full precision, not rounded). A statistic
  that has no data is `null`.
* Errors always look like:

```json
{"error": "unknown monitor id(s): 999", "status": 404}
```

| Status | When |
|--------|------|
| `200` | OK |
| `201` | Show created |
| `204` | Show deleted (empty body) |
| `400` | Bad input — missing/invalid parameter, bad timestamp, end before start, range too long. The message says which. |
| `401` | Missing, wrong or disabled API key |
| `404` | Unknown monitor id, unknown show |
| `404` | Also: unknown path under `/api/v1` |
| `405` | Wrong HTTP method for the path |
| `502` | `…/deliver` only: Spot tried to send the report and the destination failed (message included) |
| `500` | A bug in Spot — please report it with the request |

### Limits

| Limit | Value |
|-------|-------|
| Range length | 366 days |
| Monitors per request | 50 |
| Show length | 7 days |
| `max_points` (series) | 10 – 10,000 |
| PDF chart resolution | ≤ 4,000 plotted points per monitor (statistics always use every reading) |

There is no rate limiting, but a `/stats` call over a long range on a busy
sensor scans every reading in that range. For dashboards that poll, use
short ranges or `/series` with `max_points`.

---

## 4. Endpoint reference

| Method & path | Purpose |
|---|---|
| [`GET /`](#get-) | Ping / check the key, Spot version, timezone |
| [`GET /monitors`](#get-monitors) | All monitors with their latest reading |
| [`GET /stats`](#get-stats) | Statistics for monitors over a range |
| [`GET /series`](#get-series) | Readings (optionally downsampled) for charting |
| [`GET /report.pdf`](#get-reportpdf) | Branded PDF report for monitors over a range |
| [`GET /shows`](#get-shows) | List shows overlapping a window |
| [`POST /shows`](#post-shows) | Create a show, or update it by `external_id` (upsert) |
| [`GET /shows/{id}`](#get-showsid-and-get-showsrefexternal_id) · [`GET /shows/ref/{external_id}`](#get-showsid-and-get-showsrefexternal_id) | One show, with statistics once it has started |
| [`PATCH /shows/{id}`](#patch-showsid-and-patch-showsrefexternal_id) · [`PATCH /shows/ref/{external_id}`](#patch-showsid-and-patch-showsrefexternal_id) | Change some fields |
| [`DELETE /shows/{id}`](#delete-showsid-and-delete-showsrefexternal_id) · [`DELETE /shows/ref/{external_id}`](#delete-showsid-and-delete-showsrefexternal_id) | Delete a show (readings are untouched) |
| [`GET /shows/{id}/report.pdf`](#get-showsidreportpdf-and-get-showsrefexternal_idreportpdf) · [`GET /shows/ref/{external_id}/report.pdf`](#get-showsidreportpdf-and-get-showsrefexternal_idreportpdf) | The show's PDF report |
| [`POST /shows/{id}/deliver`](#post-showsiddeliver-and-post-showsrefexternal_iddeliver) · [`POST /shows/ref/{external_id}/deliver`](#post-showsiddeliver-and-post-showsrefexternal_iddeliver) | Send the report to its callback / e-mail now |

`{external_id}` is your own identifier, URL-encoded. It may contain `/`.

### `GET /`

Check connectivity and that the key works.

```json
{
  "service": "spot",
  "api": "v1",
  "version": "1.4.0",
  "key": "321Theater production",
  "timezone": "America/New_York",
  "now": "2026-10-24T17:51:53.666950-04:00"
}
```

### `GET /monitors`

Every monitor, sorted by name.

```json
{
  "monitors": [
    {
      "id": 1,
      "name": "Stage Left dB",
      "unit": "dB",
      "location": "Walt Disney Theater",
      "description": null,
      "enabled": true,
      "latest": {"ts": "2026-10-24T17:51:53.631278-04:00", "value": 102.4}
    }
  ]
}
```

| Field | Notes |
|-------|-------|
| `id` | Use this in every other call. |
| `unit` | Free text set in Spot: `dB`, `dBA`, `°F`, `%`, … Units starting with `dB` get acoustic statistics. |
| `location` | Free-text label set in Spot's UI, for humans. Not a 321Theater venue id; don't rely on it for mapping. |
| `enabled` | Disabled monitors keep their history but receive no new data. |
| `latest` | Newest reading with a value, however old; `null` if none ever. |

### `GET /stats`

Statistics for one or more monitors over a range.

```
GET /stats?monitors=1,2&start=2026-10-24T19:30:00-04:00&end=2026-10-24T22:15:00-04:00
```

```json
{
  "start": "2026-10-24T19:30:00-04:00",
  "end": "2026-10-24T22:15:00-04:00",
  "monitors": [
    {
      "id": 1,
      "name": "Stage Left dB",
      "unit": "dB",
      "location": "Walt Disney Theater",
      "stats": {
        "count": 9900,
        "events": 2,
        "min": 80.0,   "min_at": "2026-10-24T20:38:54.514-04:00",
        "max": 104.0,  "max_at": "2026-10-24T21:39:30.270-04:00",
        "avg": 98.3,
        "p10": 82.3,   "p50": 101.6, "p90": 103.5,
        "leq": 101.2,
        "l10": 103.5,  "l50": 101.6, "l90": 82.3
      }
    }
  ]
}
```

* With `detect=1`, `start` / `end` in the response are the **actual**
  range the statistics cover, and a `detection` object explains how they
  were found ([5.1](#51-finding-the-actual-start-and-end-detect)).
* Monitors come back sorted by name (match on `id`, not position).
* `leq`, `l10`, `l50`, `l90` appear **only for units starting with `dB`**.
* With no readings in the range: `count` is `0` and every value is `null`.
* Any unknown id → `404` naming the missing ids (nothing partial).

Field definitions: [section 5](#5-statistics-what-each-number-means).

### `GET /series`

Readings for drawing your own chart.

```
GET /series?monitors=1&start=…&end=…&max_points=500
```

| Parameter | Default | Meaning |
|-----------|---------|---------|
| `max_points` | `500` | If the range holds more readings than this, they are averaged into equal time buckets. 10 – 10,000. |
| `detect`, … | off | Optional — see [detection parameters](#detection-parameters). The series then covers the actual range and the response includes `detection`. |

```json
{
  "start": "…", "end": "…",
  "monitors": [
    {
      "id": 1, "name": "Stage Left dB", "unit": "dB", "color": "#e3b341",
      "bucket_seconds": 20,
      "points": [
        {"ts": "2026-10-24T19:30:10-04:00", "value": 86.48, "min": 80.0, "max": 92.1}
      ]
    }
  ]
}
```

* `bucket_seconds: null` → `points` are the raw readings (`ts`, `value`).
* `bucket_seconds: N` → each point is one *N*-second bucket: `ts` is the
  bucket's midpoint, `value` its average, `min`/`max` its extremes. Empty
  buckets are omitted (a gap in the data stays a gap).
* `color` is the colour Spot uses for that monitor, if you want to match.

### `GET /report.pdf`

Spot's branded one-page landscape PDF for any monitors and range: a chart
(one panel per unit) and a per-monitor table (samples, min, max, average,
plus Leq / L10 / L90 when dB monitors are included).

```
GET /report.pdf?monitors=1,2&start=…&end=…&title=Hamilton%20—%20Sat%20Eve
```

| Parameter | Meaning |
|-----------|---------|
| `title` | Optional heading (max 200 chars). Default: the monitor names. |
| `detect`, … | Optional — see [detection parameters](#detection-parameters). The PDF then covers the actual range, and its header lists the *Scheduled* and *Detected* times. |

Response: `200 application/pdf` with
`Content-Disposition: attachment; filename="spot_report_20261024T1930.pdf"`.
Typically 60–200 KB; generation takes well under a second.

### Shows

A **show** is a named time range plus a list of monitor ids. Shows created
through the API are tagged `API` in Spot's UI and are visible to Spot users.

#### The show object

```json
{
  "id": 1,
  "external_id": "321T-perf-8812",
  "name": "Hamilton — Saturday Evening",
  "source": "api",
  "status": "finished",
  "starts_at": "2026-10-24T19:30:00-04:00",
  "ends_at": "2026-10-24T22:15:00-04:00",
  "starts_local": "Sat 2026-10-24 19:30",
  "ends_local": "22:15",
  "duration_minutes": 165,
  "notes": null,
  "monitor_ids": [1, 2],
  "monitors": [ … see below … ],
  "email_to": "audio@example.org",
  "callback_url": "https://321theater.example.org/hooks/spot",
  "auto_detect": true,
  "detect": {"window_minutes": 15, "min_step": 6.0, "monitor": null},
  "report_start": "2026-10-24T19:34:20-04:00",
  "report_end": "2026-10-24T22:26:59-04:00",
  "detection": { …see 5.1… },
  "delivered_at": "2026-10-24T22:20:41-04:00",
  "delivery_status": "delivered",
  "delivery_attempts": 1,
  "report_pdf_url": "http://spot.internal:6100/api/v1/shows/1/report.pdf",
  "spot_url": "http://spot.internal:6100/shows/1"
}
```

| Field | Notes |
|-------|-------|
| `id` | Spot's id for the show. |
| `external_id` | **Your** id (e.g. 321Theater's performance id). Unique in Spot. Optional, but strongly recommended — it makes create idempotent and lets you address the show without storing Spot's id. One line, max 128 chars, leading/trailing spaces trimmed (also when looking it up). |
| `source` | `api` or `ui` (made by a person in Spot). |
| `status` | `upcoming` (before `starts_at`), `live`, or `finished` (after `ends_at`), computed at request time. |
| `starts_local` / `ends_local` | Human-friendly display strings; don't parse them. |
| `monitors` | In `GET` of a single show that has started: one entry per monitor with `stats` exactly as in [`/stats`](#get-stats). In list/create/update responses: `{id, name, unit, location, color}` only. |
| `email_to` | Comma-separated recipients of the post-show report, or `null`. |
| `callback_url` | Where Spot POSTs the report after the show, or `null`. See [6.1](#61-show-report-callback). |
| `auto_detect` | When `true`, the show's statistics and report cover the **actual** start/end found in the levels ([5.1](#51-finding-the-actual-start-and-end-detect)), not `starts_at`/`ends_at`. |
| `detect` | Detection settings: `window_minutes`, `min_step`, `monitor` (reference monitor id or `null` = first dB monitor). |
| `report_start`, `report_end`, `detection` | Only in single-show `GET` once it has started: the range the statistics cover, and (with `auto_detect`) the detection result; `detection` is `null` when `auto_detect` is off. |
| `delivered_at` / `delivery_status` / `delivery_attempts` | Report delivery state. `delivery_status` is `null` (not attempted), `"delivered"`, or the last error message. |
| `report_pdf_url` | Fetch the PDF any time (needs your API key). Absolute when Spot's `SPOT_PUBLIC_URL` is set; otherwise built from your request. |
| `spot_url` | Link to the show in Spot's UI (for humans). |

#### `GET /shows`

Shows that **overlap** a window, ordered by start time (max 500).

| Parameter | Default |
|-----------|---------|
| `from` | 30 days ago |
| `to` | 30 days from now |

```json
{"shows": [ {…show…}, … ]}
```

#### `POST /shows`

Create a show — or, if a show with the same `external_id` already exists,
**replace** it (upsert). Optional fields you leave out are cleared or reset
to their defaults (no notes, no e-mail, no callback, detection off), so
always send the full show. Safe to call repeatedly — even concurrently —
e.g. every time 321Theater saves the performance. To change only some
fields, use `PATCH`.

Request body (JSON):

| Field | Required | Notes |
|-------|----------|-------|
| `name` | yes | String, max 200 chars. Line breaks are collapsed to spaces. |
| `start` | yes | ISO-8601 (send an offset). |
| `end` | yes | After `start`; at most 7 days later. |
| `monitors` | yes | Non-empty array of monitor ids (integers). All must exist. |
| `external_id` | recommended | Your id; enables upsert. |
| `notes` | no | Free text. |
| `email_to` | no | Array of address strings (or a single string). Each must look like `name@host` with no spaces or commas. Needs e-mail configured in Spot. |
| `callback_url` | no | `http://` or `https://` URL for the post-show report. Must resolve; link-local / reserved addresses (e.g. `169.254.x.x`) are refused, and if Spot's `SPOT_CALLBACK_ALLOW` is set the host must be on it. |
| `auto_detect` | no | `true` to report on the actual start/end found in the levels. Default `false`. |
| `detect` | no | `{"window_minutes": 15, "min_step": 6, "monitor": 3}` — all optional; defaults shown (`monitor` defaults to the first dB monitor). Sending `detect` sets all three; omitted keys go back to their defaults. |

```http
POST /api/v1/shows
Content-Type: application/json

{
  "external_id": "321T-perf-8812",
  "name": "Hamilton — Saturday Evening",
  "start": "2026-10-24T19:30:00-04:00",
  "end": "2026-10-24T22:15:00-04:00",
  "monitors": [1, 2],
  "callback_url": "https://321theater.example.org/hooks/spot",
  "email_to": ["audio@example.org"]
}
```

Responses: **`201`** with the show object when created, **`200`** when an
existing `external_id` was updated. Validation errors return `400`:

```json
{"error": "end must be after start", "status": 400}
{"error": "monitors must be a non-empty list of monitor ids", "status": 400}
{"error": "callback_url must be an http(s) URL", "status": 400}
{"error": "unknown monitor id(s): 99", "status": 404}
```

With `auto_detect`, the report waits a little longer: until 5 minutes
after `end` **plus** the search window (default 15 min) plus 2 minutes,
so a late curtain-down is seen before the report is built.

Creating **or updating** a show re-arms report delivery: if it has already
ended, the report goes out again after the grace period. (That's what you
want after correcting times; just be aware of it.)

#### `GET /shows/{id}` and `GET /shows/ref/{external_id}`

The show object. Once the show has started (`live` or `finished`), each
entry in `monitors` includes `stats` (for a live show: from `starts_at` up
to `ends_at`, i.e. everything so far).

#### `PATCH /shows/{id}` and `PATCH /shows/ref/{external_id}`

Change only the fields you send (same names and rules as `POST`; `null` or
`""` clears `notes`, `email_to`, `callback_url`). `external_id` itself can't
be changed. Returns `200` with the show. Re-arms delivery like `POST`.

```json
{"end": "2026-10-24T22:40:00-04:00", "notes": "Ran long — encore"}
```

#### `DELETE /shows/{id}` and `DELETE /shows/ref/{external_id}`

`204`, empty body. Only the show is removed; readings are never affected.
Use this for cancelled performances.

#### `GET /shows/{id}/report.pdf` and `GET /shows/ref/{external_id}/report.pdf`

The show's PDF — same layout as `/report.pdf`, titled with the show name and
listing the external ref and locations. Can be fetched at any time,
including during the show (it covers the full scheduled range; the chart
simply ends at "now").

#### `POST /shows/{id}/deliver` and `POST /shows/ref/{external_id}/deliver`

Build and send the report immediately to the show's `callback_url` and/or
`email_to`. Returns `{"ok": true}`, or `502` with
`{"ok": false, "error": "callback: HTTP 500"}` if the destination failed.
`400` if the show has neither destination.

* **Before the report is due** (before or during the show): this is a
  **preview**. It's sent with `"final": false` in the callback metadata and
  does **not** count as delivery — the real report still goes out
  automatically after the show.
* **After it's due:** a normal delivery (`"final": true`); counts as an
  attempt and marks the show delivered on success.

---

## 5. Statistics: what each number means

All statistics are computed by Postgres over **every reading** in the range
(never from a downsampled chart).

| Field | Meaning |
|-------|---------|
| `count` | Number of readings with a numeric value. |
| `events` | Number of label-only event markers (e.g. "Show Start") in the range. |
| `min`, `max` | Lowest / highest reading. |
| `min_at`, `max_at` | When the lowest / highest reading happened (if the extreme value occurs more than once, one of those times). |
| `avg` | Arithmetic mean of the readings. |
| `p10`, `p50`, `p90` | 10th / 50th (median) / 90th percentile, linearly interpolated (`percentile_cont`). |
| `leq` | *dB units only.* **Equivalent continuous level**: 10 · log₁₀( mean( 10^(L/10) ) ). This is the standard "average loudness" — averaging decibels arithmetically (`avg`) understates it because dB is logarithmic. Expect `leq ≥ avg`. |
| `l10` | *dB units only.* Level **exceeded 10 % of the time** (= `p90`). Describes the loud passages. |
| `l50` | *dB units only.* Median level (= `p50`). |
| `l90` | *dB units only.* Level exceeded 90 % of the time (= `p10`). Describes the background / quiet floor. |

Notes:

* Readings are treated as equally weighted. Leq and the L-percentiles are
  time-based only if the sensor reports at a steady rate — true for Spot's
  polled and streaming meters. A sensor that sends readings irregularly
  will weight busy periods more heavily.
* Values are as stored (after each monitor's optional rounding in Spot).
* For `count: 0`, everything else is `null`.

### 5.1 Finding the actual start and end (`detect`)

Scheduled times are approximate — a 21:00 curtain might go up at 20:54 or
21:04. With detection on, Spot finds the real edges in the sound levels of
one **reference monitor** (a dB monitor) and reports on those.

**How it works**

1. Readings from `start − window` to `end + window` (plus 2 minutes) are
   averaged into 10-second buckets.
2. **Start:** for every moment within ±`window_minutes` of `start`, Spot
   compares the **median** level of the 2 minutes before with the median of
   the 2 minutes after. The moment with the biggest *rise* wins.
3. **End:** the same around `end`, looking for the biggest *drop*.
4. A step smaller than `min_step` (default 6 dB) isn't trusted: that edge
   **keeps the time you sent**, and the response says why.
5. The winning edge is then snapped to the exact reading: the first reading
   at show level (start) and the last one (end) — 1-second precision on a
   1 Hz meter.

Medians over two minutes mean a single clap, shout or door slam can't
trigger it; it takes a sustained change. The two searches never overlap,
even on short events. Detection on a typical show takes ~20 ms.

**The `detection` object** (returned whenever `detect=1`, and on shows with
`auto_detect`):

```json
{
  "monitor_id": 7,
  "monitor_name": "Walt Disney Theater FOH dB",
  "window_minutes": 15,
  "min_step": 6.0,
  "scheduled_start": "2026-10-23T21:00:00-04:00",
  "scheduled_end":   "2026-10-23T23:30:00-04:00",
  "start": "2026-10-23T21:04:20-04:00",
  "end":   "2026-10-23T23:40:59-04:00",
  "start_detected": true,
  "end_detected": true,
  "start_step": 22.1,
  "end_step": 28.4,
  "start_offset_seconds": 260,
  "end_offset_seconds": 659,
  "start_reason": "rise of 22.1 dB",
  "end_reason": "drop of 28.4 dB"
}
```

| Field | Meaning |
|-------|---------|
| `start`, `end` | The range actually reported on (detected, or the scheduled time where detection fell back). |
| `start_detected`, `end_detected` | `false` → that edge is the scheduled time. |
| `start_step`, `end_step` | Size of the best step found (dB), even if too small to use; `null` if there wasn't enough data. |
| `start_offset_seconds`, `end_offset_seconds` | Detected minus scheduled (negative = early). |
| `start_reason`, `end_reason` | Human-readable explanation, e.g. `"largest rise was 0.6 dB (needs 6); using the scheduled time"` or `"not enough readings near the scheduled time"`. |
| `monitor_id`, `monitor_name` | The reference monitor (`null` with reason `"no sound (dB) monitor to detect from"` if the request had none). |

**When it can't find an edge** (and falls back, with a reason):

* The show opens **quietly** after loud pre-show music — there's no rise.
  Use a smaller `min_step`, a different `detect_monitor` (e.g. a stage mic
  rather than the house), or rely on the scheduled time.
* The house music after the show is as loud as the show — no drop.
* The real edge is further away than `window_minutes`.
* The meter wasn't reporting around that time.

Intermissions are not searched — only the two edges — so a mid-show dip
never cuts a report short. Detection only changes the **range**; it never
alters readings.

**Tip for 321Theater:** if you know the real curtain times (e.g. from the
stage manager's report), send them and leave detection off — exact beats
inferred. Detection is for when you only have the published schedule.

---

## 6. What Spot sends to you

Spot makes outbound HTTP requests in two cases. Both come **from the Spot
server**, so your endpoint must be reachable from it.

### 6.1 Show-report callback

When a show has a `callback_url`, Spot POSTs its report there **about 5
minutes after `ends_at`** (the delay lets late readings arrive). Checked
once a minute, so expect it 5–6 minutes after the end.

**Request:**

```http
POST https://321theater.example.org/hooks/spot
Content-Type: multipart/form-data; boundary=spot-…
User-Agent: Spot/1.4.0
```

Two parts:

| Part | Content type | Contents |
|------|--------------|----------|
| `metadata` | `text/plain; charset=utf-8` | A JSON string (below). |
| `report` | `application/pdf` | The PDF, filename like `spot_show_Hamilton___Saturday_Evening_20261024T1930.pdf`. |

`metadata` JSON:

```json
{
  "event": "show.report",
  "show": { …the show object (section 4), without per-monitor stats… },
  "monitors": [
    {"id": 1, "name": "Stage Left dB", "unit": "dB", "location": "Walt Disney Theater",
     "stats": { …exactly as in /stats… }}
  ],
  "report_start": "2026-10-24T19:34:20-04:00",
  "report_end": "2026-10-24T22:26:59-04:00",
  "detection": null,
  "final": true
}
```

`final` is `false` only for a manual preview sent before the show's report
was due (see `…/deliver`); store those as provisional or ignore them.

`report_start` / `report_end` are the range the statistics and PDF cover —
the scheduled times, or the detected ones when the show has `auto_detect`
(then `detection` explains them, see 5.1).

Match it to your performance with `show.external_id`.

**Your response:** any **2xx** within **60 seconds** means delivered. The
body is ignored.

**Failures and retries:** a non-2xx response, timeout or connection error
is retried with exponential backoff — 1, 2, 4, 8, 16 minutes later — up to
**6 attempts in total**, then Spot gives up (visible in Spot's UI and in
`delivery_status`; `POST …/deliver` re-sends manually).

**Duplicates are possible** (a manual *Send again*, a show updated after it
ended, or a 2xx that got lost in transit). Make your handler **idempotent**:
upsert on `show.external_id` (or `show.id`) rather than inserting blindly.

If the show also has `email_to`, the same PDF is e-mailed; a failure of
either counts as a failed attempt and both are retried.

### 6.2 Alert webhook (optional)

If Spot's `SPOT_ALERT_WEBHOOK_URL` is set, every alert raise and recovery is
POSTed as JSON (`Content-Type: application/json`):

```json
{
  "event": "alert.raised",
  "alert_id": 12,
  "kind": "high",
  "message": "101.8 dB (10s average) is above the 95 dB limit",
  "monitor": {"id": 1, "name": "Stage Left dB", "unit": "dB", "location": "Walt Disney Theater"},
  "value": 101.79,
  "threshold": 95.0,
  "started_at": "2026-10-24T17:39:24-04:00",
  "cleared_at": null,
  "url": "http://spot.internal:6100/monitors/1/dashboard"
}
```

| Field | Notes |
|-------|-------|
| `event` | `alert.raised` or `alert.cleared` (same `alert_id`). Also `test` when someone clicks *Send test webhook* in Spot. |
| `kind` | `high`, `low` or `stale` (no data). |
| `value` | The value judged: the window average if the monitor averages over a window, otherwise the latest reading (which, for a `stale` alert, is the last — old — value). `null` when there's no data at all. |
| `started_at`, `cleared_at` | Eastern with offset, like every other Spot timestamp. |

One attempt per event, 15 s timeout, no retries — alerts are also in Spot's
UI and syslog. One URL is shared by all monitors.

---

## 7. Pushing data into Spot

How readings get **into** Spot. These endpoints use **per-monitor tokens**
(shown on each monitor's page in Spot), not API keys, and are outside
`/api/v1`.

| Method & path | Body |
|---|---|
| `POST /api/ingest/{token}` | JSON `{"value": 92.3, "label": "Show Start", "ts": "…"}` (all optional but one of value/label), form fields of the same names, or plain text (`92.3`, `92.3,Show Start`, `Fire Alarm`). |
| `POST /api/event/{token}` | An event marker only: JSON `{"label": "…"}`, form `label=…`, or plain text. |

Responses: `{"ok": true, "id": 123}`; `400 {"ok": false, "error": "…"}`;
`404` unknown/disabled monitor; `403` if `SPOT_INGEST_ALLOW` is set and the
source IP isn't in it.

**Event markers from 321Theater** — e.g. posting `"Act 1"`, `"Intermission"`,
`"Act 2"` to a monitor's event endpoint at the right moments — appear as
dashed markers on Spot's charts and PDFs and count in `events`.

TCP/UDP line protocols and Modbus polling are described in the main
`README.md`.

---

## 8. Recipes for 321Theater

### 8.1 Map your venues to Spot monitors

Spot doesn't know 321Theater's venues. Keep a mapping on your side, e.g.:

```json
{
  "walt-disney-theater": [1, 2],
  "pugh-theater": [3],
  "alexis-jim-pugh": [7]
}
```

Build it once from `GET /monitors` (names and `location` help a human pick)
and store it in 321Theater's configuration. Monitor ids are stable.

### 8.2 Levels for a performance (pull, no state in Spot)

```
GET /stats?monitors=1,2&start=<curtain up>&end=<curtain down>
GET /report.pdf?monitors=1,2&start=…&end=…&title=<performance name>
```

### 8.3 Have Spot deliver the report automatically (push)

1. When a performance is scheduled or changed in 321Theater:
   `POST /shows` with `external_id` = your performance id, times, the
   venue's monitor ids, and `callback_url` (and/or `email_to`). Re-sending
   on every change is fine — it upserts.
2. If it's cancelled: `DELETE /shows/ref/{external_id}`.
3. ~5 minutes after it ends, Spot POSTs the PDF + stats to your callback
   ([6.1](#61-show-report-callback)). Store them against the performance.
4. Optional safety net: a nightly job calling `GET /shows?from=…` and
   fetching `report.pdf` for any finished show you didn't receive.

### 8.4 Let Spot find the real curtain times

If 321Theater only knows the published times, create the show with
`"auto_detect": true` (or add `detect=1` to `/stats` and `/report.pdf`).
The report then covers the performance as it actually ran, and
`detection` tells you what was found — e.g. curtain up 4 minutes late,
down 11 minutes late — which you may want to store as well.

### 8.5 Actual vs scheduled times

If 321Theater learns the real curtain-down time, `PATCH` the show's `end`
before (or after) it ends. After it ends, the report is re-sent with the
corrected range — make your callback handler replace, not append.

### 8.6 A live readout in 321Theater

Poll `GET /monitors` (latest values) every 5–15 s, or
`GET /series?monitors=…&minutes=15&max_points=300` for a small chart.

---

## 9. Code examples

### curl

```bash
SPOT=http://spot.internal:6100/api/v1
KEY=spot_xxxxxxxx

curl -H "Authorization: Bearer $KEY" "$SPOT/monitors"

curl -H "Authorization: Bearer $KEY" \
  "$SPOT/stats?monitors=1,2&start=2026-10-24T19:30:00-04:00&end=2026-10-24T22:15:00-04:00"

curl -H "Authorization: Bearer $KEY" -o report.pdf \
  "$SPOT/report.pdf?monitors=1,2&start=2026-10-24T19:30:00-04:00&end=2026-10-24T22:15:00-04:00&title=Hamilton"

curl -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" -X POST "$SPOT/shows" -d '{
  "external_id": "321T-perf-8812", "name": "Hamilton — Sat Eve",
  "start": "2026-10-24T19:30:00-04:00", "end": "2026-10-24T22:15:00-04:00",
  "monitors": [1, 2], "callback_url": "https://321theater.example.org/hooks/spot"}'

curl -H "Authorization: Bearer $KEY" -X DELETE "$SPOT/shows/ref/321T-perf-8812"
```

### Python (standard library only)

```python
import json, urllib.request, urllib.parse

SPOT = "http://spot.internal:6100/api/v1"
KEY = "spot_xxxxxxxx"

def spot(method, path, params=None, body=None):
    url = SPOT + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {KEY}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
            return json.loads(data) if r.headers.get_content_type() == "application/json" else data
    except urllib.error.HTTPError as e:
        raise RuntimeError(json.loads(e.read())["error"]) from None

stats = spot("GET", "/stats", {"monitors": "1,2",
                               "start": "2026-10-24T19:30:00-04:00",
                               "end": "2026-10-24T22:15:00-04:00"})
for m in stats["monitors"]:
    print(m["name"], m["stats"]["max"], m["stats"].get("leq"))

spot("POST", "/shows", body={"external_id": "321T-perf-8812", "name": "Hamilton",
                            "start": "2026-10-24T19:30:00-04:00",
                            "end": "2026-10-24T22:15:00-04:00", "monitors": [1, 2]})
```

### JavaScript (browser or Node 18+)

```js
const SPOT = 'http://spot.internal:6100/api/v1';
const KEY = 'spot_xxxxxxxx';

async function spot(path, { method = 'GET', params, body } = {}) {
  const url = new URL(SPOT + path);
  if (params) Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
  const r = await fetch(url, {
    method,
    headers: { Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error((await r.json()).error);
  return r.status === 204 ? null : r.headers.get('content-type').includes('json') ? r.json() : r.blob();
}

const { monitors } = await spot('/stats', { params: {
  monitors: '1,2', start: '2026-10-24T19:30:00-04:00', end: '2026-10-24T22:15:00-04:00' } });
```

> Calling Spot from a browser page served by another host needs CORS, which
> Spot does not enable. Call it from 321Theater's server side (which also
> keeps the API key out of browsers).

### Receiving the show-report callback (Python / Flask)

```python
import json
from flask import Flask, request

app = Flask(__name__)

@app.post("/hooks/spot")
def spot_report():
    meta = json.loads(request.form["metadata"])
    if meta["event"] != "show.report":
        return "", 204
    show = meta["show"]
    pdf = request.files["report"].read()
    # Idempotent: replace whatever is stored for this performance.
    save_report(performance_id=show["external_id"], pdf=pdf,
                 stats={m["id"]: m["stats"] for m in meta["monitors"]})
    return "", 204
```

---

## 10. Security & operations

* **Transport:** Spot speaks plain HTTP. On anything but a trusted internal
  network, put it behind a reverse proxy with TLS (nginx, Caddy, Traefik)
  and use `https://` URLs.
* **API keys** are only for `/api/v1`. Store them like passwords; prefer one
  key per integrating system so they can be revoked independently. Spot logs
  rejected keys (`spot.api.v1`, with the source address) to syslog.
* **The web UI is unauthenticated** — anyone who can reach it can create
  API keys. Restrict it at the network or proxy level.
* **Callback URLs** are called from the Spot server. Spot never follows
  redirects (a 3xx counts as a failure), refuses link-local / reserved
  addresses such as `169.254.169.254`, re-checks the URL at send time, and
  can be restricted to specific hosts or subnets with
  `SPOT_CALLBACK_ALLOW=321theater.internal,10.20.0.0/16`.
* **Cross-site requests:** Spot's web forms refuse POSTs whose `Origin` /
  `Referer` names another site, so a malicious page can't drive an
  operator's browser (e.g. to create an API key). `/api/v1` uses keys, not
  cookies, so it isn't affected.
* **`SPOT_PUBLIC_URL`** (e.g. `http://spot.internal:6100`) makes the
  `report_pdf_url` / `spot_url` links absolute and correct in callbacks and
  e-mails. Without it, they're built from the incoming request (or relative,
  in callbacks).
* **Clock:** Spot uses the server clock (keep NTP running). Timestamps sent
  with an explicit offset are stored exactly as given.

---

## 11. Versioning policy

* The path is versioned: `/api/v1`. Within v1, changes are **additive
  only** — new endpoints, new optional parameters, new response fields.
  Clients must ignore fields they don't recognise.
* Anything that would break a v1 client (removing or renaming fields,
  changing types or meanings) would go in `/api/v2`, with v1 kept running
  alongside.
* `GET /` reports Spot's version. API changes are listed in `CHANGELOG.md`.
* The `/data/…` endpoints used by Spot's own pages are **not** part of this
  contract and may change at any time — use `/api/v1`.
