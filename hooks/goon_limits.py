"""Reading what a Claude Code transcript and the live-session registry say about a usage limit."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# "resets 2:20pm (UTC)", "resets Oct 6, 3am (Europe/Lisbon)", also with the year when it falls in the next one.
RESET = re.compile(r'resets (?:([A-Z][a-z]{2}) (\d{1,2}), (?:(\d{4}), )?)?(\d{1,2})(?::(\d{2}))?(am|pm) \(([\w/+-]+)\)')
WINDOW = re.compile(r'(session|weekly|5-hour) limit')


@dataclass(frozen=True)
class Limit:
    reset: datetime
    window: str
    at: datetime


def parse_reset(text: str, at: datetime) -> datetime | None:
    """Reset time a limit message states as UTC, or None if absent or in an unknown zone."""
    m = RESET.search(text)
    if not m:
        return None
    mon, day, year, hh, mm, ap, tz = m.groups()
    try:
        zone = timezone.utc if tz == 'UTC' else ZoneInfo(tz)
    except (KeyError, ValueError, OSError):
        return None
    now = at.astimezone(zone)
    hour, minute = int(hh) % 12 + (12 if ap == 'pm' else 0), int(mm or 0)
    if mon:
        date = datetime.strptime(f'{year or now.year} {mon} {day}', '%Y %b %d')
        d = date.replace(hour=hour, minute=minute, tzinfo=zone)
        if not year and d < now - timedelta(days=1):
            d = d.replace(year=d.year + 1)
    else:
        d = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if d <= now:
            d += timedelta(days=1)
    return d.astimezone(timezone.utc)


def row_text(row: dict) -> str:
    content = row.get('message', {}).get('content')
    return content if isinstance(content, str) else ' '.join(b.get('text', '') for b in content or [])


def limit_of(row: dict) -> Limit | None:
    """The limit a transcript row reports: its structured reset time, else the one in its text."""
    if not row.get('isApiErrorMessage'):
        return None
    at = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
    info = (row.get('apiErrorParams') or {}).get('rate_limit_info') or {}
    if isinstance(info.get('resetsAt'), (int, float)):
        return Limit(
            datetime.fromtimestamp(info['resetsAt'], timezone.utc), str(info.get('rateLimitType') or 'usage'), at
        )
    text = row_text(row)
    reset = parse_reset(text, at)
    if reset is None:
        return None
    m = WINDOW.search(text)
    return Limit(reset, m.group(1) if m else 'usage', at)


def tail_rows(path: Path) -> list[dict]:
    """Rows of the transcript's last 256 KiB, oldest first; the cut first line is dropped by its parse."""
    try:
        with path.open('rb') as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 262144))
            lines = f.read().decode('utf-8', 'replace').splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def find_limit(path: Path, now: datetime) -> Limit | None:
    """Newest usage-limit error in the transcript, if written in the last 5 minutes."""
    for row in reversed(tail_rows(path)):
        try:
            limit = limit_of(row)
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if limit and now - limit.at < timedelta(minutes=5):
            return limit
    return None


def is_blocked(path: Path) -> bool | None:
    """True while the newest message is still a limit error; None if the transcript cannot be read."""
    rows = tail_rows(path)
    if not rows:
        return None
    for row in reversed(rows):
        if row.get('type') in ('user', 'assistant') and not row.get('isSidechain'):
            return bool(row.get('isApiErrorMessage'))
    return None


def is_open(session_id: str, registry: Path) -> bool | None:
    """Whether a running Claude Code process holds the session; None if the registry cannot be read."""
    if not registry.is_dir():
        return None
    for entry in registry.glob('*.json'):
        try:
            info = json.loads(entry.read_text())
            if info.get('sessionId') == session_id:
                os.kill(int(info['pid']), 0)
                return True
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return False
