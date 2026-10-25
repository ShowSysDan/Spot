"""Which units are sound levels, set on Setup → Units.

Sound units get the calculated acoustic figures (Leq, L10, L90) wherever
summaries are shown, and their monitors are preferred as the reference for
show start/end detection. The list is stored in the `settings` table and
cached here; Spot runs as one process, so saving updates every request.
"""
from __future__ import annotations

import json
import logging

SETTING_KEY = "sound_units"
SUGGESTED = ("dB", "dBA", "dBC")

# None until a list is saved: fall back to "anything starting with dB".
_saved: frozenset[str] | None = None   # normalized, for matching
_saved_display: list[str] = []         # as entered, for the Units page


def _norm(unit: str | None) -> str:
    return (unit or "").strip().lower()


def is_sound_unit(unit: str | None) -> bool:
    """dB, dBA, dBC… — units where Leq / L10 / L90 are meaningful."""
    if _saved is None:
        return _norm(unit).startswith("db")
    return _norm(unit) in _saved


def saved_units() -> list[str] | None:
    """The saved list as entered, or None while the default rule applies."""
    return None if _saved is None else list(_saved_display)


def _apply(units: list[str] | None) -> None:
    global _saved, _saved_display
    if units is None:
        _saved, _saved_display = None, []
    else:
        _saved_display = sorted({u.strip() for u in units if u.strip()}, key=str.lower)
        _saved = frozenset(_norm(u) for u in _saved_display)


def load() -> None:
    """Read the saved list at start-up (no row → the default rule)."""
    from .db import session_scope
    from .models import Setting
    try:
        with session_scope() as s:
            row = s.get(Setting, SETTING_KEY)
            raw = row.value if row else None
        _apply(json.loads(raw) if raw else None)
    except Exception as e:  # never let a bad row stop start-up
        logging.getLogger("spot").error("could not load sound units, using default: %s", e)
        _apply(None)


def save(units: list[str]) -> None:
    from .db import session_scope
    from .models import Setting
    _apply(units)
    with session_scope() as s:
        row = s.get(Setting, SETTING_KEY) or Setting(key=SETTING_KEY)
        row.value = json.dumps(_saved_display)
        s.add(row)


def reset() -> None:
    from .db import session_scope
    from .models import Setting
    with session_scope() as s:
        row = s.get(Setting, SETTING_KEY)
        if row:
            s.delete(row)
    _apply(None)
