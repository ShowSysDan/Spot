"""Plain-language definitions of the summary statistics.

One source for the legend under every summary table (show page, reports
page, PDF exports), written for readers who aren't sound engineers — the
PDF in particular goes to management as an executive summary.
"""
from __future__ import annotations

# key → (label as shown in the table, explanation). Order is legend order.
STATS: dict[str, tuple[str, str]] = {
    "count": ("Samples", "How many readings the monitor took in this time range."),
    "min": ("Min", "The lowest single reading."),
    "max": ("Max", "The highest single reading. This can be a brief spike "
                   "rather than a typical level."),
    "avg": ("Average", "All readings added together and divided by how many "
                       "there were."),
    "leq": ("Leq", "Overall loudness: the one steady level that would deliver "
                   "the same total sound energy as the whole period. Loud "
                   "moments count for more than in a plain average, so it is "
                   "usually higher than Average. This is the standard figure to compare "
                   "against a noise limit."),
    "l10": ("L10", "The loud parts: the sound was louder than this 10% of the "
                   "time. Shows how loud the peaks of the show were without "
                   "being thrown off by one brief spike."),
    "l90": ("L90", "The background: the sound was louder than this 90% of the "
                   "time, so it is the quiet floor of the room (air handling, "
                   "audience murmur)."),
    "events": ("Events", "Markers logged during the time range, such as "
                         "\"Show Start\" or \"Fire Alarm\"."),
}

ACOUSTIC = ("leq", "l10", "l90")

# Shown with the acoustic terms: a sense of scale for decibel differences.
DB_NOTE = ("Sound levels are in decibels (dB). Every 10 dB increase sounds "
           "roughly twice as loud; a change of 3 dB is about the smallest "
           "most people notice.")


def legend(keys) -> list[tuple[str, str, str]]:
    """(key, label, explanation) for each of `keys`, in legend order."""
    return [(k, *STATS[k]) for k in STATS if k in keys]
