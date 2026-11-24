# Vendored from claude-meridiana: edit it there and run sync.py.
# body sha256: 69c117d0dd14bc9797f5354a913149843b2f5856b6e39c3f9f62b21019fd695b
"""Reading a Claude Code usage-limit error, waiting for its reset and pushing to ntfy; shared by Portavoce and Goon."""

from __future__ import annotations

import base64
import hashlib
import os
try:
    import fcntl
except ImportError:
    fcntl = None
if os.name == 'nt':
    import msvcrt
import hashlib
import http.client
import json
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
PUSH_ATTEMPTS = 3
PUSH_RETRY_WAIT = 30  # seconds between attempts
MERGE_WINDOW = timedelta(seconds=5)  # emails planned this close together are sent as one
HANDOVER_WAIT = 600  # seconds Portavoce waits for Goon to claim or send the merged email
NOTE_WAIT = 120  # seconds Goon waits for a Portavoce that is known to be coming
SENDING_WAIT = 7200  # seconds Portavoce waits for a Goon that claimed the email to finish resuming and send it
HANDOVER_POLL = 1
HANDOVER_VERSION = 1

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


def announcement(restored: list[Cap], pending: list[Cap]) -> str:
    """Which caps have reset and which still hold; a spend cap only blocks the credit fallback, not the plan."""
    names = describe_caps(restored)
    if len(restored) == 1:
        text = f'Your {names} has reset.'
    else:
        text = f'Your {names} have {"both" if len(restored) == 2 else "all"} reset.'
    holding = [cap for cap in pending if cap.kind not in CREDIT_KINDS]
    capped = [cap for cap in pending if cap.kind in CREDIT_KINDS]
    if holding:
        text += (
            ' Still in force: ' + ', '.join(f'{cap.label} (until {format_reset(cap.reset)})' for cap in holding) + '.'
        )
    else:
        text += ' You can continue.'
    if capped:
        text += f' Usage credits stay capped by your {capped[0].label} until {format_reset(capped[0].reset)}.'
    return text


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
    try:
        info = {**((row.get('apiErrorParams') or {}).get('rate_limit_info') or {}), **(row.get('quotaLimits') or {})}
        caps = caps_from_info(info, at)
    except (TypeError, ValueError, AttributeError):
        caps = []  # unreadable structured info: the message text still says when the limit resets
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
    """Newest usage-limit error in the transcript, if written in the last 5 minutes and no other error followed."""
    if not path:
        return None
    for row in reversed(tail_rows(Path(path))):
        try:
            limit = limit_of(row)
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if limit is None and row.get('isApiErrorMessage') and not row.get('isSidechain'):
            return None  # a newer error that is no limit stopped the turn; the limit before it is stale
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
        lock_path = Path(str(marker) + '.lock') if os.name == 'nt' else marker
        if os.name == 'nt' and not lock_path.exists():
            try:
                fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                os.write(fd, b'\0')
                os.close(fd)
            except FileExistsError:
                pass
        try:
            fd = os.open(lock_path, os.O_RDWR)
        except FileNotFoundError:
            continue
        try:
            if os.name == 'nt':
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            os.close(fd)
            return None
        try:
            if os.name == 'nt' or os.fstat(fd).st_ino == Path(marker).stat().st_ino:
                return fd
        except FileNotFoundError:
            pass
        os.close(fd)  # the previous holder deleted the file after we opened it; try again


def release(fd: int, marker: Path) -> None:
    if os.name == 'nt':
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)
        Path(str(marker) + '.lock').unlink(missing_ok=True)
        marker.unlink(missing_ok=True)
    else:
        os.close(fd)
        Path(marker).unlink(missing_ok=True)


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


def encode_header(text: str) -> str:
    """Non-ASCII text as an RFC 2047 word: urllib sends headers as latin-1, and ntfy decodes the word."""
    return text if text.isascii() else f'=?UTF-8?B?{base64.b64encode(text.encode()).decode()}?='


def push(
    base_url: str,
    topic: str,
    text: str,
    log: Callable[[str], None],
    *,
    title: str,
    tags: str = '',
    icon: str = '',
    email: str = '',
    token: str = '',
    signature: str = '',
) -> bool:
    """POST `text` to the ntfy topic, retrying transient failures; True once it went through. An email is signed."""
    headers = {'Title': encode_header(title)}
    if tags:
        headers['Tags'] = tags
    if icon:
        headers['Icon'] = icon  # a URL: the ntfy app on the phone downloads the image, this machine does not
    if email:
        headers['Email'] = email
        if signature:
            text = f'{text}\n\n— {signature}'
    if token:
        headers['Authorization'] = f'Bearer {token}'  # ntfy.sh refuses anonymous email
    request = urllib.request.Request(base_url + urllib.parse.quote(topic, safe=''), data=text.encode(), headers=headers)
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        try:
            urllib.request.urlopen(request, timeout=15).read()
            log(f'pushed (attempt {attempt})')
            return True
        except (OSError, ValueError, http.client.HTTPException) as e:
            log(f'push failed (attempt {attempt}): {describe(e)}')
            if is_permanent(e):
                return False
            if attempt < PUSH_ATTEMPTS:
                time.sleep(PUSH_RETRY_WAIT)
    log('gave up pushing')
    return False


def shared_dir() -> Path:
    """The folder Portavoce and Goon meet in, beside Claude Code's own files."""
    override = os.environ.get('MERIDIANA_DIR')
    return (
        Path(override)
        if override
        else Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude') / 'meridiana'
    )


def email_key(email: str) -> str:
    """A short fingerprint of an address: enough to tell two apart, not to read it back."""
    return hashlib.sha256(email.strip().lower().encode()).hexdigest()[:16]


def is_held(path: Path) -> bool:
    """True while some process holds the lock on `path`."""
    if os.name == 'nt':
        sidecar = Path(str(path) + '.lock')
        if not sidecar.exists():
            return False
        try:
            fd = os.open(sidecar, os.O_RDWR)
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            os.close(fd)
            return False
        except (BlockingIOError, OSError):
            try:
                os.close(fd)
            except UnboundLocalError:
                pass
            return True
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        return False
    except (BlockingIOError, OSError):
        return True
    finally:
        os.close(fd)


def write_new(path: Path, data: dict) -> bool:
    """Create `path` with `data` in one step; False if it already exists."""
    scratch = path.with_name(f'{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp')
    fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(json.dumps(data))
    try:
        os.link(scratch, path)
        return True
    except FileExistsError:
        return False
    finally:
        scratch.unlink(missing_ok=True)


def sweep(folder: Path, age: float = 86400) -> None:
    """Remove hand-over files a day old; an offer or intent is only removed once nothing holds it."""
    for path in folder.glob('*'):
        try:
            if time.time() - path.stat().st_mtime > age and not (
                path.suffix in ('.offer', '.intent') and is_held(path)
            ):
                path.unlink()
        except OSError:
            pass


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get('v') == HANDOVER_VERSION else None


class Held:
    """A file saying that a watcher lives and when it plans to email, locked for as long as it does."""

    suffix = ''

    def __init__(self, at: datetime, email: str, directory: Path | None = None) -> None:
        self.at = at
        self.email = email_key(email)
        self.base = directory
        self.folder = (directory or shared_dir()) / 'handover'
        self.path = self.folder / f'{int(at.timestamp())}{self.suffix}'
        self.fd: int | None = None
        self.lock_fd: int | None = None

    def open(self) -> Held:
        try:
            self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        except FileExistsError as error:
            raise NotADirectoryError(str(error)) from error
        sweep(self.folder)
        self.clear()
        if os.name == 'nt':
            self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            lock_path = Path(str(self.path) + '.lock')
            self.lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            if os.fstat(self.lock_fd).st_size == 0:
                os.write(self.lock_fd, b'\0')
            os.lseek(self.lock_fd, 0, os.SEEK_SET)
            msvcrt.locking(self.lock_fd, msvcrt.LK_LOCK, 1)
        else:
            self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        os.ftruncate(self.fd, 0)
        plan = {'v': HANDOVER_VERSION, 'at': int(self.at.timestamp()), 'email': self.email}
        os.write(self.fd, json.dumps(plan).encode())
        return self

    def clear(self) -> None:
        """Remove what a watcher that died left for this time."""

    def close(self) -> None:
        if os.name == 'nt':
            if self.lock_fd is not None:
                os.lseek(self.lock_fd, 0, os.SEEK_SET)
                msvcrt.locking(self.lock_fd, msvcrt.LK_UNLCK, 1)
                os.close(self.lock_fd)
                self.lock_fd = None
                Path(str(self.path) + '.lock').unlink(missing_ok=True)
            if self.fd is not None:
                os.close(self.fd)
                self.fd = None
            self.path.unlink(missing_ok=True)
        elif self.fd is not None:
            release(self.fd, self.path)
            self.fd = None

    def __enter__(self) -> Held:
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()


class Intent(Held):
    """Portavoce's plan to push at `at`, so that a Goon about to email knows an announcement is coming."""

    suffix = '.intent'


class Offer(Held):
    """Goon's offer to carry Portavoce's news in the email it sends at `at`."""

    suffix = '.offer'

    def __init__(self, at: datetime, email: str, directory: Path | None = None) -> None:
        super().__init__(at, email, directory)
        self.note = self.path.with_suffix('.note')
        self.verdict = self.path.with_suffix('.verdict')
        self.sent = self.path.with_suffix('.sent')

    def clear(self) -> None:
        for leftover in (self.note, self.verdict, self.sent):
            leftover.unlink(missing_ok=True)  # the verdict and the sent mark of a watcher that ended stay until then

    def read_note(self) -> tuple[list[Cap], list[Cap]] | None:
        """The caps Portavoce left to announce: those that reset, and those still in force."""
        data = read_json(self.note)
        if data is None:
            return None
        try:
            restored, pending = decode_caps(data['restored']), decode_caps(data['pending'])
        except (KeyError, TypeError, ValueError):
            return None
        return (restored, pending) if restored else None

    def wait_note(self, patience: float = NOTE_WAIT) -> tuple[list[Cap], list[Cap]] | None:
        """The note, waiting for it while a Portavoce known to be coming (its intent is held) has not left it yet."""
        deadline = time.monotonic() + patience
        while True:
            coming = find_held('.intent', self.at, self.email, self.base) is not None  # read before the note
            note = self.read_note()
            if note or not coming or time.monotonic() >= deadline:
                return note
            time.sleep(HANDOVER_POLL)

    def claim(self) -> bool:
        """Decide that Goon sends the email; False when Portavoce already decided to send its own."""
        return write_new(self.verdict, {'v': HANDOVER_VERSION, 'by': 'goon'})

    def delivered(self) -> None:
        """Tell Portavoce the email went out, even if it only looks once Goon's watcher has ended."""
        write_new(self.sent, {'v': HANDOVER_VERSION, 'at': int(self.at.timestamp()), 'email': self.email})


def try_open(held: Held, log: Callable[[str], None]) -> Held | None:
    """Open a hand-over file; None, with a line in the log, when the shared folder cannot be used."""
    try:
        return held.open()
    except OSError as error:
        log(f'no hand-over: {describe(error)}')
        held.close()
        return None


def encode_caps(caps: list[Cap]) -> list[list]:
    return [[cap.kind, int(cap.reset.timestamp())] for cap in caps]


def decode_caps(raw: list[list]) -> list[Cap]:
    return [Cap(str(kind), datetime.fromtimestamp(reset, timezone.utc)) for kind, reset in raw]


def find_held(suffix: str, at: datetime, key: str, directory: Path | None = None) -> Path | None:
    """The live file of a watcher that plans something for the address with this key within MERGE_WINDOW of `at`."""
    for path in sorted(((directory or shared_dir()) / 'handover').glob(f'*{suffix}')):
        plan = read_json(path)
        if not plan or plan.get('email') != key:
            continue
        if abs(plan.get('at', 0) - at.timestamp()) <= MERGE_WINDOW.total_seconds() and is_held(path):
            return path
    return None


def goon_already_sent(at: datetime, email: str, directory: Path | None = None) -> bool:
    """True when a Goon that has since ended emailed the same address within MERGE_WINDOW of `at`."""
    for path in (directory or shared_dir()).joinpath('handover').glob('*.sent'):
        sent = read_json(path)
        if sent and sent.get('email') == email_key(email):
            if abs(sent.get('at', 0) - at.timestamp()) <= MERGE_WINDOW.total_seconds():
                return True
    return False


def hand_over(
    at: datetime,
    restored: list[Cap],
    pending: list[Cap],
    email: str,
    log: Callable[[str], None],
    *,
    on_goon: Callable[[], None],
    directory: Path | None = None,
    wait: float = HANDOVER_WAIT,
    sending_wait: float = SENDING_WAIT,
) -> bool:
    """Leave the announcement with a Goon about to email the same address; True when Goon sends the email.
    `on_goon` runs once, as soon as the email is known to be Goon's, so the caller can still push to the phone."""
    try:
        return negotiate(at, restored, pending, email, log, on_goon, directory, wait, sending_wait)
    except OSError as error:  # the shared folder is a convenience: without it the caller sends its own email
        log(f'hand-over failed: {describe(error)}')
        return False


def negotiate(
    at: datetime,
    restored: list[Cap],
    pending: list[Cap],
    email: str,
    log: Callable[[str], None],
    on_goon: Callable[[], None],
    directory: Path | None,
    wait: float,
    sending_wait: float,
) -> bool:
    """The hand-over itself: one verdict file decides who emails, so a late answer or a timeout cannot make both."""
    offer = find_held('.offer', at, email_key(email), directory)
    if offer is None:  # checked after the offer: a Goon that ended wrote its sent mark before it ended
        sent = goon_already_sent(at, email, directory)
        if sent:
            on_goon()
        return sent
    note, verdict, sent_mark = (offer.with_suffix(suffix) for suffix in ('.note', '.verdict', '.sent'))
    if not write_new(note, {'v': HANDOVER_VERSION, 'restored': encode_caps(restored), 'pending': encode_caps(pending)}):
        return False
    log('left the announcement with Goon')
    deadline = time.monotonic() + wait
    while True:
        alive = is_held(offer)  # read before the verdict: a claim always comes before the watcher ends
        owner = (read_json(verdict) or {}).get('by')
        if owner == 'goon':
            on_goon()
            return goon_sends(offer, sent_mark, sending_wait, log)
        if not alive or time.monotonic() >= deadline:
            took = write_new(verdict, {'v': HANDOVER_VERSION, 'by': 'portavoce'})
            if took or owner is not None or not alive:
                note.unlink(missing_ok=True)
                log('Goon did not send the announcement')
                return False
        else:
            time.sleep(HANDOVER_POLL)


def goon_sends(offer: Path, sent: Path, patience: float, log: Callable[[str], None]) -> bool:
    """Wait for the Goon that claimed the email to end; True unless it ended without sending."""
    deadline = time.monotonic() + patience
    while is_held(offer):
        if time.monotonic() >= deadline:
            log('Goon is still sending; leaving the email to it')
            return True
        time.sleep(HANDOVER_POLL)
    if sent.exists():
        return True
    log('Goon could not send the announcement')
    return False
