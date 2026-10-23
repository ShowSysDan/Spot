"""Modbus TCP polling: a Read Holding Registers (FC3) client and a
per-device polling thread that dispatches decoded values to bound monitors.

Each `ModbusDevice` row gets one `ModbusPoller` thread. Per cycle the thread
reads the configured register block from the device in a single TCP
transaction, then for each enabled `Monitor` that links to the device,
decodes the appropriate slice (per `modbus_data_type` and the device's
`word_order`) and stores it as a Reading.

The TCP exchange is one-shot per cycle: connect → request → response →
disconnect. That mirrors the Q-SYS Lua reference and is the friendliest
pattern for cheap PLC/sensor TCP stacks.
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
from typing import Optional

from .config import Config
from .db import session_scope
from .ingest import MalformedData, store_reading
from .models import ModbusDevice
from .workers import WorkerRegistry

log = logging.getLogger("spot.modbus")

WORD_ORDERS: tuple[str, ...] = ("ABCD", "CDAB", "BADC", "DCBA")
DATA_TYPES: tuple[str, ...] = ("float32", "uint16", "int16", "uint32", "int32")

# Number of 16-bit registers consumed by each data type.
DATA_TYPE_REGS: dict[str, int] = {
    "float32": 2,
    "uint16": 1,
    "int16": 1,
    "uint32": 2,
    "int32": 2,
}

DEFAULT_PORT = 502
FC_READ_HOLDING = 3
MAX_REGISTERS = 125              # Modbus spec ceiling per request
MIN_POLL_INTERVAL = 1            # seconds
DEFAULT_BACKOFF_AFTER_ERROR = 10 # seconds, used if the poll loop itself crashes


class ModbusError(Exception):
    """Modbus protocol-level error (exception response, malformed frame, etc.)."""


# ---------------------------------------------------------------------------
# Frame build / parse
# ---------------------------------------------------------------------------

def build_read_holding(slave: int, addr: int, qty: int, tid: int) -> bytes:
    """Assemble a Read Holding Registers (FC3) request frame.

    MBAP header (7 bytes) + PDU (5 bytes) = 12 bytes.
    """
    if not 0 <= slave <= 255:
        raise ValueError(f"slave out of range: {slave}")
    if not 0 <= addr <= 0xFFFF:
        raise ValueError(f"address out of range: {addr}")
    if not 1 <= qty <= MAX_REGISTERS:
        raise ValueError(f"qty out of range: {qty}")
    return struct.pack(
        ">HHHBBHH",
        tid & 0xFFFF,        # transaction id
        0,                   # protocol id (always 0 for Modbus)
        6,                   # length: 6 bytes follow (unit + fc + addr + qty)
        slave,               # unit / slave id
        FC_READ_HOLDING,     # function code 3
        addr,                # starting register
        qty,                 # quantity of registers
    )


def parse_response(data: bytes, tid: int) -> list[int]:
    """Parse a Read Holding Registers response. Returns the register list."""
    if len(data) < 9:
        raise ModbusError(f"short response ({len(data)} bytes)")
    rtid, _proto, _length = struct.unpack(">HHH", data[:6])
    if rtid != (tid & 0xFFFF):
        raise ModbusError(f"transaction id mismatch (got {rtid}, want {tid})")
    fc = data[7]
    if fc & 0x80:
        ex = data[8] if len(data) > 8 else 0
        raise ModbusError(f"modbus exception (fc=0x{fc:02x}, code={ex})")
    if fc != FC_READ_HOLDING:
        raise ModbusError(f"unexpected function code 0x{fc:02x}")
    byte_count = data[8]
    if len(data) < 9 + byte_count:
        raise ModbusError(f"truncated payload ({len(data)} of {9 + byte_count} bytes)")
    if byte_count % 2:
        raise ModbusError(f"odd byte count {byte_count}")
    return list(struct.unpack(f">{byte_count // 2}H", data[9:9 + byte_count]))


def _read_full_frame(sock: socket.socket) -> bytes:
    """Read one complete Modbus TCP frame (MBAP header + body)."""
    header = b""
    while len(header) < 6:
        chunk = sock.recv(6 - len(header))
        if not chunk:
            raise ModbusError("connection closed before header")
        header += chunk
    length = struct.unpack(">H", header[4:6])[0]
    body = b""
    while len(body) < length:
        chunk = sock.recv(length - len(body))
        if not chunk:
            raise ModbusError("connection closed mid-frame")
        body += chunk
    return header + body


def read_holding_registers(host: str, port: int, slave: int, addr: int, qty: int,
                           timeout: float, tid: int) -> list[int]:
    """One-shot blocking read: connect → request → response → disconnect."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        sock.sendall(build_read_holding(slave, addr, qty, tid))
        resp = _read_full_frame(sock)
    finally:
        try:
            sock.close()
        except Exception:
            pass
    return parse_response(resp, tid)


# ---------------------------------------------------------------------------
# Decoders
# ---------------------------------------------------------------------------

def _assemble4(r1: int, r2: int, word_order: str) -> bytes:
    """Pack two 16-bit registers into 4 bytes per the device's word order.

    ABCD = straight big-endian, CDAB = word-swapped (Monigear default),
    BADC = byte-swapped, DCBA = full little-endian.
    """
    a = (r1 >> 8) & 0xFF
    b = r1 & 0xFF
    c = (r2 >> 8) & 0xFF
    d = r2 & 0xFF
    if word_order == "ABCD":
        return bytes((a, b, c, d))
    if word_order == "CDAB":
        return bytes((c, d, a, b))
    if word_order == "BADC":
        return bytes((b, a, d, c))
    if word_order == "DCBA":
        return bytes((d, c, b, a))
    raise ValueError(f"unknown word_order: {word_order}")


def decode_value(regs: list[int], offset: int, data_type: str,
                 word_order: str) -> float:
    """Decode one value of `data_type` from `regs` starting at index `offset`."""
    n = DATA_TYPE_REGS.get(data_type)
    if n is None:
        raise ValueError(f"unknown data_type: {data_type}")
    if offset < 0 or offset + n > len(regs):
        raise ValueError(f"register slice {offset}+{n} outside buffer of {len(regs)}")
    if data_type == "uint16":
        return float(regs[offset] & 0xFFFF)
    if data_type == "int16":
        v = regs[offset] & 0xFFFF
        return float(v if v < 32768 else v - 65536)
    raw = _assemble4(regs[offset], regs[offset + 1], word_order)
    if data_type == "float32":
        return float(struct.unpack(">f", raw)[0])
    if data_type == "uint32":
        return float(struct.unpack(">I", raw)[0])
    if data_type == "int32":
        return float(struct.unpack(">i", raw)[0])
    raise ValueError(f"unknown data_type: {data_type}")


# ---------------------------------------------------------------------------
# Poller thread + manager
# ---------------------------------------------------------------------------

class ModbusPoller(threading.Thread):
    """Per-device polling loop."""

    def __init__(self, device_id: int, name: str):
        super().__init__(daemon=True, name=f"modbus-{name}-{device_id}")
        self.device_id = device_id
        self.device_name = name
        # Not `_stop`: that would shadow Thread._stop() on Python <= 3.12.
        self._stop_event = threading.Event()
        self._tid = 0

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        log.info("modbus poller started: device=%s id=%d",
                 self.device_name, self.device_id)
        # Brief stagger so all pollers don't hit the network at boot.
        if self._stop_event.wait(timeout=2):
            log.info("modbus poller stopped: device=%s id=%d",
                     self.device_name, self.device_id)
            return
        while not self._stop_event.is_set():
            try:
                interval = self._poll_once()
            except Exception:
                log.exception("modbus poller error device=%s id=%d",
                              self.device_name, self.device_id)
                interval = DEFAULT_BACKOFF_AFTER_ERROR
            if self._stop_event.wait(timeout=interval):
                break
        log.info("modbus poller stopped: device=%s id=%d",
                 self.device_name, self.device_id)

    def _next_tid(self) -> int:
        self._tid = (self._tid % 65535) + 1
        return self._tid

    def _poll_once(self) -> int:
        """Run one poll cycle. Returns the seconds to wait before the next one."""
        # Snapshot config + monitors out of the session, then release the
        # connection before doing network I/O.
        with session_scope() as s:
            dev = s.get(ModbusDevice, self.device_id)
            if dev is None or not dev.enabled:
                return 30
            cfg = {
                "name": dev.name,
                "host": dev.host,
                "port": dev.port,
                "slave": dev.slave_id,
                "interval": max(MIN_POLL_INTERVAL, dev.poll_interval_seconds),
                "timeout": max(1, dev.timeout_seconds),
                "word_order": dev.word_order,
                "register_start": dev.register_start,
                "register_count": dev.register_count,
            }
            monitors = [
                {
                    "id": m.id,
                    "name": m.name,
                    "address": m.modbus_register_address,
                    "data_type": m.modbus_data_type or "float32",
                    "enabled": m.enabled,
                }
                for m in dev.monitors
            ]

        active = [m for m in monitors
                  if m["enabled"] and m["address"] is not None]
        if not active:
            return cfg["interval"]

        tid = self._next_tid()
        try:
            regs = read_holding_registers(
                cfg["host"], cfg["port"], cfg["slave"],
                cfg["register_start"], cfg["register_count"],
                cfg["timeout"], tid,
            )
        except (socket.timeout, OSError, ModbusError) as e:
            log.warning("modbus read failed device=%s host=%s:%d err=%s",
                        cfg["name"], cfg["host"], cfg["port"], e)
            return cfg["interval"]

        for mon in active:
            offset = mon["address"] - cfg["register_start"]
            try:
                value = decode_value(regs, offset, mon["data_type"],
                                     cfg["word_order"])
            except ValueError as e:
                log.warning("modbus decode failed device=%s monitor=%s err=%s",
                            cfg["name"], mon["name"], e)
                continue
            try:
                store_reading(mon["id"], value, None)
                log.debug("modbus ok device=%s monitor=%s value=%s",
                          cfg["name"], mon["name"], value)
            except MalformedData as e:
                log.warning("modbus store failed device=%s monitor=%s err=%s",
                            cfg["name"], mon["name"], e)
        return cfg["interval"]


class ModbusManager:
    """One ModbusPoller thread per enabled Modbus device."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._workers: WorkerRegistry[ModbusPoller] = WorkerRegistry()

    def start_enabled(self) -> None:
        with session_scope() as s:
            for d in s.query(ModbusDevice).filter(ModbusDevice.enabled.is_(True)).all():
                self.start_device(d.id, d.name)

    def start_device(self, device_id: int, name: str) -> None:
        self._workers.spawn(device_id, lambda: ModbusPoller(device_id, name))

    def stop_device(self, device_id: int) -> None:
        self._workers.stop(device_id)

    def shutdown(self) -> None:
        self._workers.shutdown()

    def status(self) -> dict[int, bool]:
        return self._workers.status()


def poll_now(device_id: int) -> tuple[Optional[list[int]], Optional[str]]:
    """Synchronous one-shot read for the 'Poll now' button.

    Returns (registers, None) on success or (None, error_msg) on failure.
    """
    with session_scope() as s:
        dev = s.get(ModbusDevice, device_id)
        if dev is None:
            return None, "device not found"
        host, port = dev.host, dev.port
        slave = dev.slave_id
        addr = dev.register_start
        qty = dev.register_count
        timeout = max(1, dev.timeout_seconds)
    try:
        regs = read_holding_registers(host, port, slave, addr, qty, timeout, 1)
        return regs, None
    except (socket.timeout, OSError, ModbusError) as e:
        return None, str(e)
