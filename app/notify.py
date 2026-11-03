"""Outgoing HTTP notifications: JSON webhooks (alerts) and multipart
callbacks (show reports).

Every send has a short timeout and reports failure as a string instead of
raising, so a dead endpoint can never stall the thread that called it.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import socket
import urllib.error
import urllib.parse
import urllib.request
import uuid

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


def _build_opener() -> urllib.request.OpenerDirector:
    """An opener that speaks http(s) only.

    `urllib.request.build_opener()` also installs file://, ftp:// and data:
    handlers, which would let a crafted URL read local files from the
    server. Building the OpenerDirector by hand keeps them out entirely.
    """
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(),
                    urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(),
                    urllib.request.HTTPDefaultErrorHandler(), _NoRedirect(),
                    urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


_opener = _build_opener()
_ALLOWED_SCHEMES = ("http", "https")


def _send(url: str, method: str, body: bytes | None, headers: dict[str, str],
          timeout: float, what: str | None) -> tuple[int | None, str | None]:
    """Send one request. Returns (status, error): error is None on a 2xx
    response; status is None when no response arrived at all. Never raises.
    Failures are logged as `what` (None: the caller logs them itself)."""
    def fail(status: int | None, err: str) -> tuple[int | None, str]:
        if what:
            log.error("%s failed url=%s err=%s", what, url, err)
        return status, err

    if urllib.parse.urlsplit(url).scheme.lower() not in _ALLOWED_SCHEMES:
        return fail(None, "only http:// and https:// URLs are allowed")
    headers = {"User-Agent": f"Spot/{__version__}", **headers}
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            resp.read(65536)  # drain a little so keep-alive peers are happy; bounded
            return resp.status, None
    except urllib.error.HTTPError as e:
        e.close()
        return fail(e.code, f"HTTP {e.code}")
    except (OSError, ValueError, http.client.HTTPException) as e:
        return fail(None, str(e) or e.__class__.__name__)


def post_json(url: str, payload: dict, timeout: float = HTTP_TIMEOUT) -> str | None:
    """POST `payload` as JSON. Returns None on a 2xx response, else an error."""
    body = json.dumps(payload, default=str).encode("utf-8")
    return _send(url, "POST", body, {"Content-Type": "application/json"}, timeout, "webhook")[1]


def http_request(url: str, method: str, body: bytes | None,
                 timeout: float = HTTP_TIMEOUT) -> tuple[int | None, str | None]:
    """Send one HTTP request. Returns (status code, error): error is None on
    a 2xx response; status is None when no response arrived at all.

    A body starting with { or [ is sent as JSON, anything else as text.
    """
    headers = {}
    if body:
        json_like = body.lstrip()[:1] in (b"{", b"[")
        headers["Content-Type"] = ("application/json" if json_like
                                   else "text/plain; charset=utf-8")
    return _send(url, method, body, headers, timeout, None)  # alert_actions logs


def _header_value(text: str) -> str:
    """A string safe inside a quoted multipart header parameter."""
    return "".join(c for c in text if c >= " " and c not in '"\\\x7f')


def post_multipart(url: str, fields: dict[str, str],
                   files: list[tuple[str, str, bytes, str]],
                   timeout: float = 60) -> str | None:
    """POST multipart/form-data. `files` are (field, filename, bytes, mime).

    Returns None on a 2xx response, else an error message.
    """
    boundary = f"spot-{uuid.uuid4().hex}"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{_header_value(name)}"\r\n'
            f"Content-Type: text/plain; charset=utf-8\r\n\r\n".encode() + value.encode() + b"\r\n")
    for field, filename, data, mime in files:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{_header_value(field)}"; '
            f'filename="{_header_value(filename)}"\r\nContent-Type: {_header_value(mime)}\r\n\r\n'
            .encode() + data + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return _send(url, "POST", b"".join(parts),
                 {"Content-Type": f"multipart/form-data; boundary={boundary}"},
                 timeout, "multipart POST")[1]
