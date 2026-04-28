from __future__ import annotations

import logging
import socket

log = logging.getLogger("spot.portalloc")

PORT_RANGE_START = 6101
PORT_RANGE_END = 6199  # inclusive


def can_bind(port: int, proto: str) -> bool:
    """Vibe check — try to actually bind the port; return True if successful."""
    sock_type = socket.SOCK_STREAM if proto == "tcp" else socket.SOCK_DGRAM
    s = socket.socket(socket.AF_INET, sock_type)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def allocate_port(proto: str, taken: set[int]) -> int | None:
    """Find a port in 6101-6199 that isn't already claimed and is bindable."""
    for port in range(PORT_RANGE_START, PORT_RANGE_END + 1):
        if port in taken:
            continue
        if can_bind(port, proto):
            log.info("allocated %s port %d", proto, port)
            return port
        log.debug("port %d (%s) failed vibe check", port, proto)
    log.error("no %s ports available in %d-%d (taken: %s)",
              proto, PORT_RANGE_START, PORT_RANGE_END, sorted(taken))
    return None
