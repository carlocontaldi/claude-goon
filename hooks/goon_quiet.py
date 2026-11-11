"""Quiet hours: a daily window such as `22:00-07:00` or `22:00-07:00 Europe/Lisbon` in which nothing is resumed."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

QUIET = re.compile(r'^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})(?:\s+([\w/+-]+))?\s*$')


def parse_quiet(spec: str) -> tuple[time, time, tzinfo | None] | None:
    """The window a spec describes, or None when it is empty or not a valid window."""
    m = QUIET.match(spec)
    if not m:
        return None
    sh, sm, eh, em, zone = m.groups()
    try:
        start, end = time(int(sh), int(sm)), time(int(eh), int(em))
        return (start, end, ZoneInfo(zone) if zone else None) if start != end else None
    except (ValueError, KeyError, OSError):
        return None


def at(day: date, clock: time, zone: tzinfo | None) -> datetime:
    return datetime.combine(day, clock, tzinfo=zone) if zone else datetime.combine(day, clock).astimezone()


def after_quiet(t: datetime, spec: str) -> datetime:
    """`t`, or the end of the quiet-hours window it falls in; no window or an invalid one leaves it."""
    window = parse_quiet(spec)
    if window is None:
        return t
    start, end, zone = window
    today = t.astimezone(zone).date()
    for day in (today - timedelta(days=1), today):
        opens = at(day, start, zone)
        closes = at(day if end > start else day + timedelta(days=1), end, zone)
        if opens <= t < closes:
            return closes
    return t
