"""Outgoing notifications: e-mail (SMTP) and JSON webhooks.

Used by the alert engine and by scheduled / show reports. Every send has a
short timeout and reports failure as a string instead of raising, so a dead
mail server or webhook can never stall the thread that called it.
"""
from __future__ import annotations

import json
import logging
import smtplib
import ssl
import urllib.error
import urllib.request
from email.message import EmailMessage

from .config import Config
from .version import __version__

log = logging.getLogger("spot.notify")

SMTP_TIMEOUT = 15
HTTP_TIMEOUT = 15


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
    msg = EmailMessage()
    msg["From"] = cfg.smtp_from
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["X-Mailer"] = f"Spot {__version__}"
    msg.set_content(body)
    for filename, data, mime in attachments or []:
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
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


def post_json(url: str, payload: dict, timeout: float = HTTP_TIMEOUT) -> str | None:
    """POST `payload` as JSON. Returns None on a 2xx response, else an error."""
    req = urllib.request.Request(
        url, data=json.dumps(payload, default=str).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": f"Spot/{__version__}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read(65536)
    except urllib.error.HTTPError as e:
        log.error("multipart POST failed url=%s status=%s", url, e.code)
        return f"HTTP {e.code}"
    except (OSError, ValueError) as e:
        log.error("multipart POST failed url=%s err=%s", url, e)
        return str(e) or e.__class__.__name__
    return None
