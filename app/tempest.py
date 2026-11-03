"""WeatherFlow Tempest weather stations: a UDP listener per station that
decodes the hub's broadcast messages and feeds the bound monitors.

The Tempest hub broadcasts JSON over UDP (port 50222 by default; see
https://apidocs.tempestwx.com/reference/tempest-udp-broadcast). One
`TempestStation` row describes one UDP port Spot listens on, plus an
optional list of serial numbers to accept. Every message is decoded into
named sensor fields (the catalogue below, e.g. `obs_st.air_temperature`);
each `Monitor` bound to the station names one field and gets a reading
whenever a message carrying that field arrives. Monitors may ask for the
value in another unit (°F instead of °C, mph instead of m/s, …).

The listener keeps the latest decoded value of every field and a few
counters in memory, so the station page can show what is arriving before
any monitor is bound.
"""
from __future__ import annotations

import json
import logging
import math
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from .config import Config
from .db import session_scope
from .ingest import MalformedData, store_reading
from .models import Monitor, TempestStation
from .workers import WorkerRegistry

log = logging.getLogger("spot.tempest")

DEFAULT_PORT = 50222
MAX_DATAGRAM = 8192
# Bound monitors are re-read from the database this often (seconds); a
# save in the UI also refreshes them straight away.
MONITOR_CACHE_SECONDS = 10
# A message timestamp further than this from the server clock is replaced
# with the arrival time, so a hub with a wrong clock can't file readings in
# the past or the future.
MAX_CLOCK_SKEW_SECONDS = 300


# ---------------------------------------------------------------------------
# Unit conversions
# ---------------------------------------------------------------------------

def _identity(v: float) -> float:
    return v


# kind -> {unit: converter from the native (first) unit}. The first entry
# of each is the unit the hub sends.
CONVERSIONS: dict[str, dict[str, Callable[[float], float]]] = {
    "temp": {"°C": _identity, "°F": lambda c: c * 9 / 5 + 32},
    "speed": {"m/s": _identity, "mph": lambda v: v * 2.236936,
              "km/h": lambda v: v * 3.6, "kn": lambda v: v * 1.943844},
    "pressure": {"mb": _identity, "hPa": _identity, "inHg": lambda v: v * 0.02952998},
    "distance": {"km": _identity, "mi": lambda v: v * 0.621371},
    "rain": {"mm": _identity, "in": lambda v: v / 25.4},
}


# ---------------------------------------------------------------------------
# Message and field catalogue
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MessageType:
    key: str          # "type" value in the JSON
    title: str
    array: str | None  # key holding the value array(s): obs / ob / evt, or None
    rows: bool         # True when `array` is a list of rows (obs), False for one row
    interval: int | None  # typical seconds between messages; None = event


MESSAGE_TYPES: tuple[MessageType, ...] = (
    MessageType("obs_st", "Tempest observation (every minute)", "obs", True, 60),
    MessageType("rapid_wind", "Rapid wind (every 3 seconds)", "ob", False, 3),
    MessageType("evt_strike", "Lightning strike event", "evt", False, None),
    MessageType("evt_precip", "Rain start event", "evt", False, None),
    MessageType("obs_air", "AIR observation (every minute)", "obs", True, 60),
    MessageType("obs_sky", "SKY observation (every minute)", "obs", True, 60),
    MessageType("device_status", "Device status (every minute)", None, False, 60),
    MessageType("hub_status", "Hub status (every 10 seconds)", None, False, 10),
)
MESSAGE_TYPE_MAP: dict[str, MessageType] = {m.key: m for m in MESSAGE_TYPES}


@dataclass(frozen=True)
class Field:
    key: str                 # "<message type>.<name>"
    msg: str                 # message type key
    label: str
    unit: str                # native unit as the hub sends it ("" = plain number)
    kind: str = ""           # CONVERSIONS key; "" = no conversion offered
    index: int | None = None  # position in the value row
    attr: str | None = None   # top-level JSON key (status messages)
    derive: str | None = None  # computed from the row (see _derive)
    event_label: str | None = None  # label stored with each reading (events)
    note: str = ""

    @property
    def units(self) -> tuple[str, ...]:
        """Units this field can be stored in, native first."""
        if self.kind:
            return tuple(CONVERSIONS[self.kind])
        return (self.unit,) if self.unit else ("value",)

    @property
    def interval(self) -> int | None:
        return MESSAGE_TYPE_MAP[self.msg].interval


def _obs_st_fields() -> list[Field]:
    m = "obs_st"
    return [
        Field(f"{m}.wind_lull", m, "Wind lull", "m/s", "speed", index=1),
        Field(f"{m}.wind_avg", m, "Wind average", "m/s", "speed", index=2),
        Field(f"{m}.wind_gust", m, "Wind gust", "m/s", "speed", index=3),
        Field(f"{m}.wind_direction", m, "Wind direction", "°", index=4),
        Field(f"{m}.pressure", m, "Station pressure", "mb", "pressure", index=6),
        Field(f"{m}.air_temperature", m, "Air temperature", "°C", "temp", index=7),
        Field(f"{m}.humidity", m, "Relative humidity", "%", index=8),
        Field(f"{m}.illuminance", m, "Illuminance", "lux", index=9),
        Field(f"{m}.uv", m, "UV index", "index", index=10),
        Field(f"{m}.solar_radiation", m, "Solar radiation", "W/m²", index=11),
        Field(f"{m}.rain_minute", m, "Rain in the last minute", "mm", "rain", index=12),
        Field(f"{m}.precip_type", m, "Precipitation type", "", index=13,
              note="0 = none, 1 = rain, 2 = hail, 3 = rain + hail"),
        Field(f"{m}.strike_distance", m, "Lightning strike average distance", "km",
              "distance", index=14),
        Field(f"{m}.strike_count", m, "Lightning strike count", "", index=15),
        Field(f"{m}.battery", m, "Battery", "V", index=16),
        Field(f"{m}.dew_point", m, "Dew point (calculated)", "°C", "temp",
              derive="dew_point:7:8"),
    ]


def _obs_air_fields() -> list[Field]:
    m = "obs_air"
    return [
        Field(f"{m}.pressure", m, "Station pressure", "mb", "pressure", index=1),
        Field(f"{m}.air_temperature", m, "Air temperature", "°C", "temp", index=2),
        Field(f"{m}.humidity", m, "Relative humidity", "%", index=3),
        Field(f"{m}.strike_count", m, "Lightning strike count", "", index=4),
        Field(f"{m}.strike_distance", m, "Lightning strike average distance", "km",
              "distance", index=5),
        Field(f"{m}.battery", m, "Battery", "V", index=6),
        Field(f"{m}.dew_point", m, "Dew point (calculated)", "°C", "temp",
              derive="dew_point:2:3"),
    ]


def _obs_sky_fields() -> list[Field]:
    m = "obs_sky"
    return [
        Field(f"{m}.illuminance", m, "Illuminance", "lux", index=1),
        Field(f"{m}.uv", m, "UV index", "index", index=2),
        Field(f"{m}.rain_minute", m, "Rain in the last minute", "mm", "rain", index=3),
        Field(f"{m}.wind_lull", m, "Wind lull", "m/s", "speed", index=4),
        Field(f"{m}.wind_avg", m, "Wind average", "m/s", "speed", index=5),
        Field(f"{m}.wind_gust", m, "Wind gust", "m/s", "speed", index=6),
        Field(f"{m}.wind_direction", m, "Wind direction", "°", index=7),
        Field(f"{m}.battery", m, "Battery", "V", index=8),
        Field(f"{m}.solar_radiation", m, "Solar radiation", "W/m²", index=10),
        Field(f"{m}.precip_type", m, "Precipitation type", "", index=12,
              note="0 = none, 1 = rain, 2 = hail, 3 = rain + hail"),
    ]


FIELDS: tuple[Field, ...] = tuple(
    _obs_st_fields()
    + [
        Field("rapid_wind.speed", "rapid_wind", "Wind speed", "m/s", "speed", index=1),
        Field("rapid_wind.direction", "rapid_wind", "Wind direction", "°", index=2),
        Field("evt_strike.distance", "evt_strike", "Strike distance", "km", "distance",
              index=1, event_label="Lightning strike"),
        Field("evt_strike.energy", "evt_strike", "Strike energy", "", index=2,
              event_label="Lightning strike"),
        Field("evt_precip.start", "evt_precip", "Rain started", "", derive="marker",
              event_label="Rain started",
              note="An event marker with no number; shows as a line on the chart."),
    ]
    + _obs_air_fields()
    + _obs_sky_fields()
    + [
        Field("device_status.voltage", "device_status", "Sensor battery", "V", attr="voltage"),
        Field("device_status.rssi", "device_status", "Sensor radio signal (RSSI)", "dBm",
              attr="rssi"),
        Field("device_status.hub_rssi", "device_status", "Hub radio signal (RSSI)", "dBm",
              attr="hub_rssi"),
        Field("device_status.uptime", "device_status", "Sensor uptime", "s", attr="uptime"),
        Field("device_status.sensor_status", "device_status", "Sensor status flags", "",
              attr="sensor_status", note="0 = all sensors OK; see the Tempest UDP reference."),
        Field("hub_status.rssi", "hub_status", "Hub Wi-Fi signal (RSSI)", "dBm", attr="rssi"),
        Field("hub_status.uptime", "hub_status", "Hub uptime", "s", attr="uptime"),
    ]
)
FIELD_MAP: dict[str, Field] = {f.key: f for f in FIELDS}


def fields_by_message() -> list[tuple[MessageType, list[Field]]]:
    """The catalogue grouped for a form's <optgroup>s, in MESSAGE_TYPES order."""
    return [(mt, [f for f in FIELDS if f.msg == mt.key]) for mt in MESSAGE_TYPES]


def catalogue() -> list[dict[str, Any]]:
    """JSON-friendly catalogue for the monitor form's unit picker."""
    return [{"key": f.key, "msg": f.msg, "label": f.label, "units": list(f.units),
             "note": f.note} for f in FIELDS]


def convert(field: Field, value: float, unit: str) -> float:
    """`value` (native units) in `unit`; an unknown unit leaves it as sent."""
    if field.kind:
        fn = CONVERSIONS[field.kind].get(unit)
        if fn is not None:
            return fn(value)
    return value


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Decoded:
    key: str
    value: float | None
    ts: datetime
    label: str | None = None


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def dew_point(temp_c: float, rh: float) -> float | None:
    """Magnus formula; good to about 0.4 °C in ordinary conditions."""
    if rh <= 0:
        return None
    a, b = 17.62, 243.12
    gamma = math.log(rh / 100.0) + a * temp_c / (b + temp_c)
    return b * gamma / (a - gamma)


def _derive(field: Field, row: list[Any]) -> float | None:
    what, _, args = field.derive.partition(":")
    if what == "marker":
        return None
    if what == "dew_point":
        ti, hi = (int(x) for x in args.split(":"))
        t = _num(row[ti]) if ti < len(row) else None
        h = _num(row[hi]) if hi < len(row) else None
        if t is None or h is None:
            return None
        return dew_point(t, h)
    return None


def _message_ts(epoch: Any, now: datetime) -> datetime:
    """The message's own time when it is plausible, else `now`."""
    e = _num(epoch)
    if e is None or abs(e - now.timestamp()) > MAX_CLOCK_SKEW_SECONDS:
        return now
    return datetime.fromtimestamp(e, tz=timezone.utc)


def decode(message: dict[str, Any], now: datetime | None = None) -> list[Decoded]:
    """Every catalogue field present in one hub message, with values in
    native units. Unknown message types decode to nothing."""
    now = now or datetime.now(timezone.utc)
    mtype = message.get("type")
    mt = MESSAGE_TYPE_MAP.get(mtype) if isinstance(mtype, str) else None
    if mt is None:
        return []
    fields = [f for f in FIELDS if f.msg == mt.key]
    out: list[Decoded] = []

    if mt.array is None:
        ts = _message_ts(message.get("timestamp"), now)
        for f in fields:
            v = _num(message.get(f.attr))
            if v is not None:
                out.append(Decoded(f.key, v, ts))
        return out

    payload = message.get(mt.array)
    if not isinstance(payload, list):
        return out
    rows = payload if mt.rows else [payload]
    for row in rows:
        if not isinstance(row, list) or not row:
            continue
        ts = _message_ts(row[0], now)
        for f in fields:
            if f.derive:
                v = _derive(f, row)
                if v is None and f.derive != "marker":
                    continue
            elif f.index is not None and f.index < len(row):
                v = _num(row[f.index])
                if v is None:
                    continue  # e.g. SKY local-day rain is always null over UDP
            else:
                continue
            out.append(Decoded(f.key, v, ts, f.event_label))
    return out


def parse_serials(raw: str | None) -> set[str]:
    return {s.strip().upper() for s in (raw or "").replace("\n", ",").split(",") if s.strip()}


def message_matches(message: dict[str, Any], serials: set[str]) -> bool:
    if not serials:
        return True
    for k in ("serial_number", "hub_sn"):
        v = message.get(k)
        if isinstance(v, str) and v.strip().upper() in serials:
            return True
    return False


# ---------------------------------------------------------------------------
# Listener thread + manager
# ---------------------------------------------------------------------------

class TempestListener(threading.Thread):
    """Receives one station's UDP messages and stores readings for its monitors."""

    def __init__(self, station_id: int, name: str, port: int, serials: set[str]):
        super().__init__(daemon=True, name=f"tempest-{name}-{port}")
        self.station_id = station_id
        self.station_name = name
        self.port = port
        self.serials = serials
        # Not `_stop` or `_handle`: Thread uses those names itself.
        self._stop_event = threading.Event()
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()
        self._monitors: list[dict[str, Any]] = []
        self._monitors_loaded = 0.0
        self._refresh = threading.Event()
        # In-memory picture of what is arriving, for the station page.
        self.started_at = datetime.now(timezone.utc)
        self.packets = 0
        self.ignored = 0          # wrong serial / not JSON / unknown type
        self.last_packet_at: datetime | None = None
        self.last_from: str | None = None
        self.by_type: dict[str, int] = {}
        self.devices: dict[str, dict[str, Any]] = {}   # serial -> {type, last}
        self.latest: dict[str, tuple[float | None, datetime]] = {}

    def stop(self) -> None:
        self._stop_event.set()
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def refresh_monitors(self) -> None:
        """Re-read the bound monitors before the next message."""
        self._refresh.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "started_at": self.started_at,
                "packets": self.packets,
                "ignored": self.ignored,
                "last_packet_at": self.last_packet_at,
                "last_from": self.last_from,
                "by_type": dict(self.by_type),
                "devices": {k: dict(v) for k, v in self.devices.items()},
                "latest": dict(self.latest),
            }

    # -- receive loop -------------------------------------------------------

    def run(self) -> None:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", self.port))
            srv.settimeout(1.0)
            self._sock = srv
            log.info("tempest listener started: station=%s port=%d", self.station_name, self.port)
        except OSError as e:
            log.error("tempest listener failed to bind station=%s port=%d err=%s",
                      self.station_name, self.port, e)
            return
        try:
            while not self._stop_event.is_set():
                try:
                    data, addr = srv.recvfrom(MAX_DATAGRAM)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    self._on_datagram(data, f"{addr[0]}:{addr[1]}")
                except Exception:
                    log.exception("tempest handler error station=%s", self.station_name)
        finally:
            try:
                srv.close()
            except Exception:
                pass
            log.info("tempest listener stopped: station=%s port=%d", self.station_name, self.port)

    def _on_datagram(self, data: bytes, src: str) -> None:
        now = datetime.now(timezone.utc)
        with self._lock:
            self.packets += 1
            self.last_packet_at = now
            self.last_from = src
        try:
            message = json.loads(data.decode("utf-8", errors="replace"))
        except ValueError:
            with self._lock:
                self.ignored += 1
            log.warning("tempest: not JSON station=%s src=%s payload=%r",
                        self.station_name, src, data[:120])
            return
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            with self._lock:
                self.ignored += 1
            return
        mtype = message["type"]
        if not message_matches(message, self.serials):
            with self._lock:
                self.ignored += 1
            log.debug("tempest: ignored %s from unlisted device station=%s serial=%s hub=%s",
                      mtype, self.station_name, message.get("serial_number"),
                      message.get("hub_sn"))
            return
        decoded = decode(message, now)
        with self._lock:
            self.by_type[mtype] = self.by_type.get(mtype, 0) + 1
            serial = message.get("serial_number")
            if isinstance(serial, str):
                self.devices[serial] = {"type": mtype, "last": now,
                                        "hub": message.get("hub_sn")}
            for d in decoded:
                self.latest[d.key] = (d.value, d.ts)
        if mtype not in MESSAGE_TYPE_MAP:
            log.debug("tempest: unknown message type %r station=%s", mtype, self.station_name)
            return
        if not decoded:
            return
        monitors = self._bound_monitors()
        if not monitors:
            return
        by_key: dict[str, list[Decoded]] = {}
        for d in decoded:
            by_key.setdefault(d.key, []).append(d)
        for mon in monitors:
            for d in by_key.get(mon["field"], ()):
                field = FIELD_MAP[d.key]
                value = convert(field, d.value, mon["unit"]) if d.value is not None else None
                try:
                    store_reading(mon["id"], value, d.label, d.ts)
                    log.debug("tempest ok station=%s monitor=%s field=%s value=%s",
                              self.station_name, mon["name"], d.key, value)
                except MalformedData as e:
                    log.warning("tempest store failed station=%s monitor=%s err=%s",
                                self.station_name, mon["name"], e)

    def _bound_monitors(self) -> list[dict[str, Any]]:
        if (self._refresh.is_set()
                or time.monotonic() - self._monitors_loaded > MONITOR_CACHE_SECONDS):
            self._refresh.clear()
            with session_scope() as s:
                rows = s.query(Monitor).filter(
                    Monitor.tempest_station_id == self.station_id,
                    Monitor.enabled.is_(True),
                    Monitor.tempest_field.is_not(None),
                ).all()
                self._monitors = [
                    {"id": m.id, "name": m.name, "field": m.tempest_field, "unit": m.unit}
                    for m in rows if m.tempest_field in FIELD_MAP
                ]
            self._monitors_loaded = time.monotonic()
        return self._monitors


class TempestManager:
    """One TempestListener thread per enabled station."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._workers: WorkerRegistry[TempestListener] = WorkerRegistry()

    def start_enabled(self) -> None:
        with session_scope() as s:
            for st in s.query(TempestStation).filter(TempestStation.enabled.is_(True)).all():
                self.start_station(st.id, st.name, st.port, st.serials)

    def start_station(self, station_id: int, name: str, port: int, serials: str | None) -> None:
        wanted = parse_serials(serials)
        self._workers.spawn(
            station_id, lambda: TempestListener(station_id, name, port, wanted))

    def stop_station(self, station_id: int) -> None:
        self._workers.stop(station_id)

    def refresh(self, station_id: int | None) -> None:
        """Tell a station's listener its monitors changed."""
        if station_id is None:
            return
        t = self._workers.get(station_id)
        if t is not None:
            t.refresh_monitors()

    def shutdown(self) -> None:
        self._workers.shutdown()

    def status(self) -> dict[int, bool]:
        return self._workers.status()

    def snapshot(self, station_id: int) -> Optional[dict[str, Any]]:
        t = self._workers.get(station_id)
        return t.snapshot() if t is not None and t.is_alive() else None
