from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from .db import session_scope
from .models import Reading

log = logging.getLogger("spot.ingest")


class MalformedData(ValueError):
    pass


def parse_payload(raw: str) -> tuple[Optional[float], Optional[str]]:
    """Parse a message into (value, label).

    Accepted forms:
      - "92.3"                     -> value only
      - "92.3,Show Start"          -> value + label
      - "92.3 Show Start"          -> value + label (space separator)
      - "Fire Alarm"               -> label-only event marker (value=None)
      - {"value": 92.3, "label": "Show Start"}  -> JSON
    """
    text = (raw or "").strip()
    if not text:
        raise MalformedData("empty payload")

    # JSON form
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError as e:
            raise MalformedData(f"invalid JSON: {e}") from e
        v = obj.get("value")
        lbl = obj.get("label")
        if v is None and not lbl:
            raise MalformedData("JSON must contain 'value' or 'label'")
        if v is not None:
            try:
                v = float(v)
            except (TypeError, ValueError) as e:
                raise MalformedData(f"value not numeric: {v!r}") from e
        return v, (str(lbl) if lbl is not None else None)

    # Numeric, possibly with label after , or whitespace
    for sep in (",", " ", "\t"):
        if sep in text:
            head, _, tail = text.partition(sep)
            head = head.strip()
            tail = tail.strip()
            try:
                v = float(head)
                return v, (tail or None)
            except ValueError:
                break  # treat the whole string as a label

    # Numeric only
    try:
        return float(text), None
    except ValueError:
        # Pure label/event marker
        return None, text


def store_reading(monitor_id: int, value: Optional[float], label: Optional[str],
                  ts: Optional[datetime] = None) -> int:
    if value is None and not label:
        raise MalformedData("reading must have a value or label")
    with session_scope() as s:
        r = Reading(
            monitor_id=monitor_id,
            ts=ts or datetime.now(timezone.utc),
            value=value,
            label=label,
        )
        s.add(r)
        s.flush()
        return r.id


def ingest_raw(monitor_id: int, monitor_name: str, raw: str, source: str) -> int:
    try:
        v, lbl = parse_payload(raw)
        rid = store_reading(monitor_id, v, lbl)
        log.debug("ingest ok monitor=%s src=%s value=%s label=%s id=%s",
                  monitor_name, source, v, lbl, rid)
        return rid
    except MalformedData as e:
        log.warning("malformed data monitor=%s src=%s err=%s payload=%r",
                    monitor_name, source, e, raw[:200])
        raise
