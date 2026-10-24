"""Outgoing notifications: e-mail (SMTP) and JSON webhooks.

Used by the alert engine and by scheduled / show reports. Every send has a
short timeout and reports failure as a string instead of raising, so a dead
mail server or webhook can never stall the thread that called it.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import smtplib
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage

from .config import Config
from .version import __version__

log = logging.getLogger("spot.notify")

SMTP_TIMEOUT = 15
HTTP_TIMEOUT = 15


def one_line(text: str) -> str:
    """Collapse CR/LF (and other line breaks) to spaces."""
    return " ".join(str(text).split()) if text else ""


# A single address: no whitespace, commas or angle brackets, exactly one "@".
EMAIL_RE = r"[^@\s,<>]+@[^@\s,<>]+"


def email_enabled(cfg: Config) -> bool:
    return bool(cfg.smtp_host)


def send_email(cfg: Config, to: list[str] | tuple[str, ...], subject: str, body: str,
               attachments: list[tuple[str, bytes, str]] | None = None) -> str | None:
    """Send a plain-text e-mail. `attachments` are (filename, bytes, mime type).

    Returns None on success, or an error message.
    """
    recipients = [r for r in to if r]
    if not email_enabled(cfg):
        return "e-mail is not configured (set SPOT_SMTP_HOST)"
    if not recipients:
        return "no recipients"
    try:
        msg = EmailMessage()
        msg["From"] = cfg.smtp_from
        msg["To"] = ", ".join(recipients)
        # Header values may not contain line breaks; names come from users.
        msg["Subject"] = one_line(subject)
        msg["X-Mailer"] = f"Spot {__version__}"
        msg.set_content(body)
        for filename, data, mime in attachments or []:
            maintype, _, subtype = mime.partition("/")
            msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    except (ValueError, TypeError) as e:
        log.error("e-mail not built subject=%r to=%s err=%s", subject, recipients, e)
        return f"could not build message: {e}"
    try:
        if cfg.smtp_security == "ssl":
            server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=SMTP_TIMEOUT,
                                      context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=SMTP_TIMEOUT)
        with server:
            if cfg.smtp_security == "starttls":
                server.starttls(context=ssl.create_default_context())
            if cfg.smtp_user:
                server.login(cfg.smtp_user, cfg.smtp_password)
            server.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        log.error("e-mail failed subject=%r to=%s err=%s", subject, recipients, e)
        return str(e) or e.__class__.__name__
    log.info("e-mail sent subject=%r to=%s", subject, recipients)
    return None


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
