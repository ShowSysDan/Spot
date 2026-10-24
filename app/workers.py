"""Thread bookkeeping shared by the TCP/UDP listener and Modbus managers.

Both keep one long-lived worker thread per id (monitor id or device id),
need to start it unless it's already running, stop + join it, stop all of
them at shutdown, and report which are alive. This class is that registry.
"""
from __future__ import annotations

import threading
from typing import Callable, Generic, Protocol, TypeVar


class StoppableThread(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def join(self, timeout: float | None = None) -> None: ...
    def is_alive(self) -> bool: ...


T = TypeVar("T", bound=StoppableThread)

JOIN_TIMEOUT = 3.0


class WorkerRegistry(Generic[T]):
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._threads: dict[int, T] = {}

    def spawn(self, key: int, factory: Callable[[], T]) -> None:
        """Start a worker for `key` unless one is already alive. A dead
        worker (e.g. a listener that failed to bind) is replaced."""
        with self._lock:
            existing = self._threads.get(key)
            if existing and existing.is_alive():
                return
            t = factory()
            self._threads[key] = t
        t.start()

    def stop(self, key: int) -> None:
        with self._lock:
            t = self._threads.pop(key, None)
        if t:
            t.stop()
            t.join(timeout=JOIN_TIMEOUT)

    def shutdown(self) -> None:
        with self._lock:
            threads = list(self._threads.values())
            self._threads.clear()
        for t in threads:
            t.stop()
        for t in threads:
            t.join(timeout=JOIN_TIMEOUT)

    def status(self) -> dict[int, bool]:
        with self._lock:
            return {key: t.is_alive() for key, t in self._threads.items()}
