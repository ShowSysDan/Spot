"""Outgoing HTTP notifications: JSON webhooks (alerts) and multipart
callbacks (show reports).

Every send has a short timeout and reports failure as a string instead of
raising, so a dead endpoint can never stall the thread that called it.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request

from .config import Config
from .version import __version__

log = logging.getLogger("spot.notify")

HTTP_TIMEOUT = 15


def one_line(text: str) -> str:
    """Collapse CR/LF (and other line breaks) to spaces."""
    return " ".join(str(text).split()) if text else ""


def callback_url_problem(url: str, cfg: Config) -> str | None:
    """Why Spot won't POST to `url`, or None if it's acceptable.

    Callbacks normally go to systems on the internal network (321Theater),
    so private and loopback addresses are allowed. Always refused:
    non-http(s) URLs, link-local / cloud-metadata addresses (169.254.0.0/16,
    fe80::/10) and unspecified addresses. With SPOT_CALLBACK_ALLOW set, the
    host must also match one of its entries (hostnames or CIDR ranges).
    """
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "callback_url must be an http(s) URL with a host"
    host = parts.hostname.lower()
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
        addrs = {ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos}
    except (OSError, ValueError):
        return f"callback_url host {host!r} does not resolve"
    for a in addrs:
        if a.is_link_local or a.is_unspecified or a.is_multicast:
            return f"callback_url may not point at {a} (link-local / reserved address)"
    if cfg.callback_allow:
        def allowed(entry: str) -> bool:
            try:
                net = ipaddress.ip_network(entry, strict=False)
                return all(a in net for a in addrs)
            except ValueError:
                return host == entry.lower()
        if not any(allowed(e) for e in cfg.callback_allow):
            return f"callback_url host {host!r} is not in SPOT_CALLBACK_ALLOW"
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Outbound webhooks/callbacks never follow redirects: the URL configured
    is the URL that gets the data."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def post_json(url: str, payload: dict, timeout: float = HTTP_TIMEOUT) -> str | None:
    """POST `payload` as JSON. Returns None on a 2xx response, else an error."""
    req = urllib.request.Request(
        url, data=json.dumps(payload, default=str).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": f"Spot/{__version__}"},
    )
    try:
        with _opener.open(req, timeout=timeout) as resp:
            resp.read(65536)
    except urllib.error.HTTPError as e:
        log.error("webhook failed url=%s status=%s", url, e.code)
        return f"HTTP {e.code}"
    except (OSError, ValueError) as e:
        log.error("webhook failed url=%s err=%s", url, e)
        return str(e) or e.__class__.__name__
    return None


def post_multipart(url: str, fields: dict[str, str],
                   files: list[tuple[str, str, bytes, str]],
                   timeout: float = 60) -> str | None:
    """POST multipart/form-data. `files` are (field, filename, bytes, mime).

    Returns None on a 2xx response, else an error message.
    """
    import uuid
    boundary = f"spot-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n'
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n".encode() + value.encode() + b"\r\n")
    for field, filename, data, mime in files:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; '
            f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        url, data=b"".join(parts), method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                 "User-Agent": f"Spot/{__version__}"},
    )
    try:
        with _opener.open(req, timeout=timeout) as resp:
            resp.read(65536)
    except urllib.error.HTTPError as e:
        log.error("multipart POST failed url=%s status=%s", url, e.code)
        return f"HTTP {e.code}"
    except (OSError, ValueError) as e:
        log.error("multipart POST failed url=%s err=%s", url, e)
        return str(e) or e.__class__.__name__
    return None
