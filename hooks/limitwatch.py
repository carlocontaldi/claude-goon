# Vendored from claude-meridiana: edit it there and run sync.py.
# body sha256: 96a14f89b621d1b91fddb31573ebee2eb54d034a69d3196403e157af7200ef9a
"""Reading a Claude Code usage-limit error, waiting for its reset and pushing to ntfy; shared by Portavoce and Goon."""

from __future__ import annotations

import fcntl
import http.client
import json
import os
import re
import secrets
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

MARGIN = timedelta(seconds=60)  # pad past the stated time so nothing happens before the limit has lifted
RECENT = timedelta(minutes=5)  # a limit error older than this is not the one that stopped the turn
WINDOW_WAIT = timedelta(days=8)
DAY, WEEK = timedelta(days=1), timedelta(days=7)
CREDIT_WAIT = timedelta(days=35)  # a spend cap may reset weekly or monthly

# rateLimitType in the error's rate_limit_info, and the label Claude Code itself gives each limit.
KINDS = {
    'five_hour': 'five_hour',
    'seven_day': 'weekly',
    'seven_day_opus': 'opus',
    'seven_day_sonnet': 'sonnet',
    'seven_day_overage_included': 'fable',
    'overage': 'credits',
}
LABELS = {
    'five_hour': '5-hour limit',
    'weekly': 'weekly limit',
    'opus': 'Opus limit',
    'sonnet': 'Sonnet limit',
    'fable': 'Fable limit',
    'spend': 'individual spend limit',
    'spend_daily': 'daily spend limit',
    'spend_weekly': 'weekly spend limit',
    'monthly': 'monthly spend limit',
    'credits': 'usage credit limit',
}
# Caps on the usage credits only: the plan itself is usable again once its window resets.
CREDIT_KINDS = frozenset({'spend', 'spend_daily', 'spend_weekly', 'monthly', 'credits'})
# overageDisabledReason while a spend cap blocks the credits; its period (daily, weekly or monthly) is not in the error.
# A weekly or monthly spend cap is told from how far off its reset is, so a reset at most a day away stays unnamed.
SPEND_REASONS = {'org_spend_cap_reached': 'spend', 'org_level_disabled_until': 'monthly'}

# "resets 2:20pm (UTC)", "resets Oct 6, 3am (Europe/Lisbon)", also with the year when it falls in the next one.
RESET = re.compile(r'resets (?:([A-Z][a-z]{2}) (\d{1,2}), (?:(\d{4}), )?)?(\d{1,2})(?::(\d{2}))?(am|pm) \(([\w/+-]+)\)')
# "You've hit your 5-hour limit · resets 9:30pm (UTC)"; an org spend-limit message adds "your session limit resets ..."
WINDOW = re.compile(r'(session|5-hour|weekly|opus|sonnet|fable) limit\s*(?:·\s*)?resets', re.I)
TEXT_KINDS = {
    'session': 'five_hour',
    '5-hour': 'five_hour',
    'weekly': 'weekly',
    'opus': 'opus',
    'sonnet': 'sonnet',
    'fable': 'fable',
}
CREDIT_TEXTS = (
    (re.compile(r'individual (?:spend|usage) limit', re.I), 'spend'),
    (re.compile(r'monthly (?:spend|usage) limit', re.I), 'monthly'),
)


@dataclass(frozen=True)
class Cap:
    kind: str
    reset: datetime

    @property
    def label(self) -> str:
        return LABELS.get(self.kind, f'{self.kind.replace("_", " ")} limit')

    @property
    def max_wait(self) -> timedelta:
        return CREDIT_WAIT if self.kind in CREDIT_KINDS else WINDOW_WAIT


@dataclass(frozen=True)
class Limit:
    """What stopped a turn: `caps[0]` is the limit the error names, the others are caps it also reports."""

    caps: tuple[Cap, ...]
    at: datetime

    @property
    def reset(self) -> datetime:
        return self.caps[0].reset

    @property
    def window(self) -> str:
        return self.caps[0].kind


def describe_caps(caps: list[Cap]) -> str:
    """The caps' labels as prose: 'A', 'A and B', 'A, B and C'."""
    labels = [cap.label for cap in caps]
    return labels[0] if len(labels) == 1 else f'{", ".join(labels[:-1])} and {labels[-1]}'


def format_reset(reset: datetime) -> str:
    utc = reset.astimezone(timezone.utc)
    return f'{utc:%b} {utc.day}, {utc:%H:%M} UTC'


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


def epoch(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def spend_kind(info: dict, reset: datetime, at: datetime | None) -> str:
    """A spend cap's kind with its period: from the error's own flags, else from how far off the reset is."""
    monthly = info.get('overagePeriodMonthly')
    if info.get('dailyCapPaused'):
        return 'spend_daily'
    if isinstance(monthly, dict) and (monthly.get('utilization') or 0) >= 1:
        return 'monthly'
    if at is None:
        return 'spend'
    wait, utc = reset - at, reset.astimezone(timezone.utc)
    if wait > WEEK + timedelta(hours=1) or (utc.day, utc.hour, utc.minute) == (1, 0, 0):
        return 'monthly'  # a weekly cap is never further off than a week; a monthly one resets at the turn of the month
    return 'spend_weekly' if wait > DAY else 'spend'


def caps_from_info(info: dict, at: datetime | None = None) -> list[Cap]:
    """The caps a rate_limit_info reports: the limit it names, plus the spend cap when that blocks the credits."""
    raw = info.get('rateLimitType')
    kind = KINDS.get(raw, str(raw or 'usage'))
    spend = SPEND_REASONS.get(info.get('overageDisabledReason'))
    overage_reset = epoch(info.get('overageResetsAt'))
    if spend == 'spend' and overage_reset:
        spend = spend_kind(info, overage_reset, at)
    if kind == 'credits' and spend:
        kind = spend  # the credit limit itself is the one reached: name it as Claude Code does
    reset = epoch(info.get('resetsAt')) or (overage_reset if kind in CREDIT_KINDS else None)
    caps = [Cap(kind, reset)] if reset else []
    if spend and overage_reset and kind not in CREDIT_KINDS:
        caps.append(Cap(spend, overage_reset))
    return caps


def cap_from_text(text: str, at: datetime) -> Cap | None:
    """The limit a message words (with a clock time and zone), for an error row without rate_limit_info."""
    reset = parse_reset(text, at)
    if reset is None:
        return None
    named = WINDOW.search(text)
    if named:
        word = named.group(1).lower()
        return Cap(TEXT_KINDS[word], reset)
    return Cap(next((kind for pattern, kind in CREDIT_TEXTS if pattern.search(text)), 'usage'), reset)


def row_text(row: dict) -> str:
    content = row.get('message', {}).get('content')
    return content if isinstance(content, str) else ' '.join(b.get('text', '') for b in content or [])


def limit_of(row: dict) -> Limit | None:
    """The limit a transcript row reports, from its structured rate_limit_info, else from its text."""
    if not row.get('isApiErrorMessage'):
        return None
    at = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
    info = {**((row.get('apiErrorParams') or {}).get('rate_limit_info') or {}), **(row.get('quotaLimits') or {})}
    caps = caps_from_info(info, at)
    if not caps and (cap := cap_from_text(row_text(row), at)):
        caps = [cap]
    return Limit(tuple(caps), at) if caps else None


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


def find_limit(path: Path | str | None, now: datetime) -> Limit | None:
    """Newest usage-limit error in the transcript, if written in the last 5 minutes."""
    if not path:
        return None
    for row in reversed(tail_rows(Path(path))):
        try:
            limit = limit_of(row)
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if limit and now - limit.at < RECENT:
            return limit
    return None


def lookup(path: Path | str | None, tries: int) -> Limit | None:
    """`find_limit`, retried once a second: the error may not be in the transcript yet when the hook fires."""
    for attempt in range(tries):
        limit = find_limit(path, datetime.now(timezone.utc))
        if limit or attempt == tries - 1:
            return limit
        time.sleep(1)
    return None


TOPIC = re.compile(r'[-_A-Za-z0-9]{1,64}')  # what ntfy allows in a topic name
TOPIC_ALPHABET = string.ascii_letters + string.digits  # no - or _, so a double-click selects the whole name
TOPIC_RANDOM_CHARS = 22  # 22 x log2(62) = 131 random bits


def read_topic(path: Path) -> str:
    try:
        saved = Path(path).read_text().strip()
    except OSError:
        return ''
    return saved if TOPIC.fullmatch(saved) else ''


def stored_topic(path: Path, prefix: str, *, rotate: bool = False) -> str:
    """The private topic saved at `path`: `<prefix>-` and 131 random bits, made on first use or again if `rotate`."""
    path = Path(path)
    if not rotate and (saved := read_topic(path)):
        return saved
    topic = f'{prefix}-' + ''.join(secrets.choice(TOPIC_ALPHABET) for _ in range(TOPIC_RANDOM_CHARS))
    scratch = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    with os.fdopen(os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
        f.write(topic + '\n')
    scratch.replace(path)
    return read_topic(path) or topic  # of two first uses racing, both end up with the one that was saved last


def effective_topic(option: str | None, path: Path, prefix: str) -> str:
    """The plugin option when it holds a name, else the saved private topic."""
    return (option or '').strip() or stored_topic(path, prefix)


def subscribe_steps(topic: str, test_command: str) -> str:
    """What to tell the person once a topic exists: how to subscribe the ntfy app, and how to try it."""
    return (
        f'In the ntfy app tap +, enter {topic} and keep the server ntfy.sh. '
        f'On Android you can open ntfy://ntfy.sh/{topic} instead. Then run {test_command}.'
    )


def claim(marker: Path) -> int | None:
    """Lock the marker for one reset time and return its fd; None if a live watcher holds it."""
    while True:
        fd = os.open(marker, os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        try:
            if os.fstat(fd).st_ino == Path(marker).stat().st_ino:
                return fd
        except FileNotFoundError:
            pass
        os.close(fd)  # the previous holder deleted the file after we opened it; try again


def release(fd: int, marker: Path) -> None:
    Path(marker).unlink(missing_ok=True)  # while still locked, so a waiting claim() sees the file gone
    os.close(fd)


def sleep_until(t: datetime) -> None:
    while (left := (t - datetime.now(timezone.utc)).total_seconds()) > 0:
        time.sleep(min(left, 60))  # re-check the wall clock so a clock jump cannot oversleep


def is_permanent(error: Exception) -> bool:
    """A bad URL or header, or a 4xx other than 429, fails the same way every time."""
    if isinstance(error, urllib.error.HTTPError):
        return 400 <= error.code < 500 and error.code != 429
    return isinstance(error, (ValueError, http.client.InvalidURL))


def describe(error: Exception) -> str:
    """Class (and HTTP status) only: an error message can quote the topic or the token."""
    return f'HTTP {error.code}' if isinstance(error, urllib.error.HTTPError) else type(error).__name__


def push(
    base_url: str,
    topic: str,
    text: str,
    log: Callable[[str], None],
    *,
    title: str,
    tags: str,
    email: str = '',
    token: str = '',
) -> bool:
    """POST `text` to the ntfy topic, retrying transient failures; True once it went through."""
    headers = {'Title': title, 'Tags': tags}
    if email:
        headers['Email'] = email
    if token:
        headers['Authorization'] = f'Bearer {token}'  # ntfy.sh refuses anonymous email
    request = urllib.request.Request(base_url + urllib.parse.quote(topic, safe=''), data=text.encode(), headers=headers)
    for attempt in range(1, 4):
        try:
            urllib.request.urlopen(request, timeout=15).read()
            log(f'pushed (attempt {attempt})')
            return True
        except (OSError, ValueError, http.client.HTTPException) as e:
            log(f'push failed (attempt {attempt}): {describe(e)}')
            if is_permanent(e):
                return False
            time.sleep(30)
    log('gave up pushing')
    return False
