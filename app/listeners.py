from __future__ import annotations

import logging
import socket
import threading
from typing import Dict

from .config import Config
from .db import session_scope
from .ingest import MalformedData, ingest_raw
from .models import Monitor

log = logging.getLogger("spot.listeners")

# Caps on a single line / datagram to bound memory. Lines longer than this
# are dropped (and logged as malformed).
MAX_LINE_BYTES = 8192
RECV_CHUNK = 4096
MAX_DATAGRAM = 8192
TCP_IDLE_TIMEOUT = 60.0


class _BaseListener(threading.Thread):
    proto: str = ""

    def __init__(self, monitor_id: int, monitor_name: str, port: int,
                 value_regex: str | None = None):
        super().__init__(daemon=True, name=f"{self.proto}-{monitor_name}-{port}")
        self.monitor_id = monitor_id
        self.monitor_name = monitor_name
        self.port = port
        self.value_regex = value_regex
        self._stop = threading.Event()
        self._sock: socket.socket | None = None

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass

    def _ingest_line(self, line: bytes, src: str) -> None:
        if not line:
            return
        if len(line) > MAX_LINE_BYTES:
            log.warning("dropping oversized %s line (%d bytes) monitor=%s src=%s",
                        self.proto, len(line), self.monitor_name, src)
            return
        text = line.decode("utf-8", errors="replace").strip()
        if not text:
            return
        try:
            ingest_raw(self.monitor_id, self.monitor_name, text, src,
                       regex=self.value_regex)
        except MalformedData:
            pass  # already logged
        except Exception:
            log.exception("%s ingest error monitor=%s", self.proto, self.monitor_name)


class TCPListener(_BaseListener):
    proto = "tcp"

    def run(self) -> None:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", self.port))
            srv.listen(8)
            srv.settimeout(1.0)
            self._sock = srv
            log.info("TCP listener started: monitor=%s port=%d", self.monitor_name, self.port)
        except OSError as e:
            log.error("TCP listener failed to bind monitor=%s port=%d err=%s",
                      self.monitor_name, self.port, e)
            return

        try:
            while not self._stop.is_set():
                try:
                    conn, addr = srv.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                threading.Thread(
                    target=self._handle_conn, args=(conn, addr), daemon=True,
                    name=f"tcp-conn-{self.monitor_name}",
                ).start()
        finally:
            try:
                srv.close()
            except Exception:
                pass
            log.info("TCP listener stopped: monitor=%s port=%d", self.monitor_name, self.port)

    def _handle_conn(self, conn: socket.socket, addr) -> None:
        src = f"tcp:{addr[0]}:{addr[1]}"
        try:
            conn.settimeout(TCP_IDLE_TIMEOUT)
            buf = bytearray()
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(RECV_CHUNK)
                except socket.timeout:
                    break
                except OSError:
                    break
                if not chunk:
                    break
                buf.extend(chunk)
                # Bound buffer; if a peer never sends a newline, drop and reset.
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(buf[:nl])
                    del buf[:nl + 1]
                    self._ingest_line(line, src)
                if len(buf) > MAX_LINE_BYTES:
                    log.warning("TCP line cap exceeded; resetting buffer monitor=%s src=%s",
                                self.monitor_name, src)
                    buf.clear()
            if buf:
                self._ingest_line(bytes(buf), src)
        finally:
            try:
                conn.close()
            except Exception:
                pass


class UDPListener(_BaseListener):
    proto = "udp"

    def run(self) -> None:
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("0.0.0.0", self.port))
            srv.settimeout(1.0)
            self._sock = srv
            log.info("UDP listener started: monitor=%s port=%d", self.monitor_name, self.port)
        except OSError as e:
            log.error("UDP listener failed to bind monitor=%s port=%d err=%s",
                      self.monitor_name, self.port, e)
            return

        try:
            while not self._stop.is_set():
                try:
                    data, addr = srv.recvfrom(MAX_DATAGRAM)
                except socket.timeout:
                    continue
                except OSError:
                    break
                src = f"udp:{addr[0]}:{addr[1]}"
                for raw in (data.splitlines() or [data]):
                    self._ingest_line(raw, src)
        finally:
            try:
                srv.close()
            except Exception:
                pass
            log.info("UDP listener stopped: monitor=%s port=%d", self.monitor_name, self.port)


class ListenerManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._threads: Dict[int, _BaseListener] = {}

    def start_enabled(self) -> None:
        with session_scope() as s:
            for m in s.query(Monitor).filter(Monitor.enabled.is_(True)).all():
                if m.listener_type in ("tcp", "udp") and m.port:
                    self._spawn(m.id, m.name, m.listener_type, m.port, m.value_regex)

    def start_monitor(self, monitor_id: int, name: str, listener_type: str,
                      port: int | None, value_regex: str | None = None) -> None:
        if listener_type not in ("tcp", "udp") or not port:
            return
        self._spawn(monitor_id, name, listener_type, port, value_regex)

    def stop_monitor(self, monitor_id: int) -> None:
        with self._lock:
            t = self._threads.pop(monitor_id, None)
        if t:
            t.stop()
            t.join(timeout=3.0)

    def shutdown(self) -> None:
        with self._lock:
            threads = list(self._threads.values())
            self._threads.clear()
        for t in threads:
            t.stop()
        for t in threads:
            t.join(timeout=3.0)

    def _spawn(self, monitor_id: int, name: str, listener_type: str, port: int,
               value_regex: str | None = None) -> None:
        with self._lock:
            existing = self._threads.get(monitor_id)
            if existing and existing.is_alive():
                return
            cls = TCPListener if listener_type == "tcp" else UDPListener
            t = cls(monitor_id, name, port, value_regex)
            self._threads[monitor_id] = t
        t.start()

    def status(self) -> dict[int, bool]:
        with self._lock:
            return {mid: t.is_alive() for mid, t in self._threads.items()}
