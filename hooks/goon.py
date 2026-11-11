#!/usr/bin/env python3
"""StopFailure hook: resume sessions a usage limit interrupted, once it resets (`reset` serves the in-session mod)."""

from __future__ import annotations

import argparse
import fcntl
import http.client
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from goon_limits import Limit, find_limit, is_blocked, is_open
from goon_quiet import after_quiet, parse_quiet

logger = logging.getLogger('goon')

CLAUDE_DIR = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
DIR = Path(os.environ.get('GOON_DIR') or CLAUDE_DIR / 'goon')
NTFY_URL = (os.environ.get('GOON_NTFY_URL') or 'https://ntfy.sh').rstrip('/') + '/'
MARGIN = timedelta(seconds=60)  # pad past the stated time so nothing starts before the limit has lifted
STAGGER = 15  # seconds between launches: each resume re-reads a whole conversation
MAX_RESUMES = 4  # per session and day, so a session that keeps hitting a limit is not looped forever
LOOKUP_TRIES = 10  # the limit error may not be in the transcript yet when the hook fires
MAX_WAIT = timedelta(days=8)
DEFAULT_RESUME_TEXT = 'Go on.'
PROMPT_WAIT = 24 * 3600  # seconds a resumed session may sit on a permission prompt before its run is ended
PERMISSION_MODES = ('acceptEdits', 'auto', 'dontAsk', 'plan', 'manual')  # never bypassPermissions
OFF_EXIT = 3  # `power` exit code while off; 1 and 2 are a crash and a usage error. register.ts holds a copy


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
            if os.fstat(fd).st_ino == marker.stat().st_ino:
                return fd
        except FileNotFoundError:
            pass
        os.close(fd)  # the previous holder deleted the file after we opened it; try again


def release(fd: int, marker: Path) -> None:
    marker.unlink(missing_ok=True)  # while still locked, so a waiting claim() sees the file gone
    os.close(fd)


def sleep_until(t: datetime) -> None:
    while (left := (t - datetime.now(timezone.utc)).total_seconds()) > 0:
        time.sleep(min(left, 60))  # re-check the wall clock so a clock jump cannot oversleep


def register(epoch: int, payload: dict) -> None:
    """Record the interrupted session under its reset time for that reset's watcher."""
    folder = DIR / 'pending' / str(epoch)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    record = {'session_id': payload['session_id'], 'cwd': payload['cwd'], 'transcript_path': payload['transcript_path']}
    target = folder / f'{payload["session_id"]}.json'
    scratch = target.with_suffix('.tmp')
    scratch.write_text(json.dumps(record))
    scratch.replace(target)


def take_pending(epoch: int) -> list[dict]:
    records = []
    for entry in sorted((DIR / 'pending' / str(epoch)).glob('*.json')):
        records.append(json.loads(entry.read_text()))
        entry.unlink()
    return records


def allow_resume(session_id: str, now: datetime) -> bool:
    """Count a resume of the session; False once MAX_RESUMES fall in one day."""
    path = DIR / 'resumes' / f'{session_id}.json'
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamps = []
    if path.exists():
        try:
            stamps = [datetime.fromisoformat(s) for s in json.loads(path.read_text())]
        except (ValueError, TypeError):
            logger.info('%s: resume history unreadable, starting it over', session_id[:8])
    stamps = [s for s in stamps if now - s < timedelta(days=1)]
    if len(stamps) >= MAX_RESUMES:
        return False
    scratch = path.with_suffix('.tmp')
    scratch.write_text(json.dumps([s.isoformat() for s in [*stamps, now]]))
    scratch.replace(path)
    return True


def claude_binary() -> str | None:
    """The claude executable, looked up now: an extension update can remove the one that armed us."""
    for candidate in (
        os.environ.get('GOON_CLAUDE'),
        os.environ.get('CLAUDE_CODE_EXECPATH'),
        shutil.which('claude'),
        str(Path.home() / '.local/bin/claude'),
    ):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    return None


def read_settings() -> dict[str, str]:
    names = ('resume_text', 'resume_permission_mode', 'quiet_hours', 'ntfy_topic', 'notify_email', 'ntfy_token')
    return {n: os.environ.get(f'CLAUDE_PLUGIN_OPTION_{n.upper()}', '').strip() for n in names}


def resume_argv(binary: str, session_id: str, settings: dict[str, str]) -> list[str]:
    """Headless resume over stream-json: permission prompts then come to us instead of being refused."""
    argv = [binary, '--resume', session_id, '-p', '--input-format', 'stream-json', '--output-format', 'stream-json']
    argv += ['--verbose', '--permission-prompt-tool', 'stdio']
    if settings['resume_permission_mode'] in PERMISSION_MODES:
        argv += ['--permission-mode', settings['resume_permission_mode']]
    return argv


def user_message(text: str) -> str:
    return json.dumps({'type': 'user', 'message': {'role': 'user', 'content': text}}) + '\n'


def supervise(job: dict, settings: dict[str, str]) -> int:
    """Run one headless resume to its end, holding a permission prompt open rather than answering it."""
    name = job['session_id'][:8]
    runs = DIR / 'runs'
    runs.mkdir(mode=0o700, parents=True, exist_ok=True)
    out = os.open(runs / f'{name}-{job["epoch"]}.log', os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        process = subprocess.Popen(
            resume_argv(job['binary'], job['session_id'], settings),
            cwd=job['cwd'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=out,
            text=True,
        )
    finally:
        os.close(out)
    process.stdin.write(user_message(settings['resume_text'] or DEFAULT_RESUME_TEXT))
    process.stdin.flush()
    timer = None
    for line in process.stdout:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        request = event.get('request') or {}
        if event.get('type') == 'control_request' and request.get('subtype') == 'can_use_tool':
            if timer is None:
                tool = str(request.get('tool_name', 'a tool'))
                logger.info('%s: waiting on a permission prompt for %s', name, tool)
                push(f'A resumed session is waiting on a permission prompt for {tool}.', settings)
                timer = threading.Timer(PROMPT_WAIT, process.kill)
                timer.start()
        elif event.get('type') == 'result':
            logger.info('%s: finished: %s', name, str(event.get('result'))[:120])
            break
    process.stdin.close()  # the engine waits for more input after a result, and exits on its end
    code = process.wait()
    process.stdout.close()
    if timer:
        timer.cancel()
    return code


def is_enabled() -> bool:
    return not (DIR / 'off').exists()


def power(word: str) -> str:
    """Switch Goon on or off ('status' only reads the switch) and say which it is."""
    flag = DIR / 'off'
    if word == 'on':
        flag.unlink(missing_ok=True)
    elif word == 'off':
        flag.touch(mode=0o600)
    return 'Goon is on.' if is_enabled() else 'Goon is off: nothing is resumed until /goon on.'


def decide(record: dict) -> str:
    """What to do with an interrupted session at the reset: an outcome label, 'resume' to start it."""
    if not is_enabled():
        return 'off'
    blocked = is_blocked(Path(record['transcript_path']))
    if blocked is None:
        return 'unreadable'
    if not blocked:
        return 'continued'
    opened = is_open(record['session_id'], CLAUDE_DIR / 'sessions')
    if opened is None:
        return 'unknown'
    if opened:
        return 'open'
    if not Path(record['cwd']).is_dir():
        return 'no-cwd'
    return 'resume'


def resume_one(record: dict, epoch: int, pause: int) -> str:
    """Return the outcome for one pending session; an error in it is logged and becomes 'failed'."""
    try:
        outcome = decide(record)
        if outcome != 'resume':
            return outcome
        time.sleep(pause)
        return launch(record, epoch)
    except Exception:
        logger.exception('%s: failed', record['session_id'][:8])
        return 'failed'


def resume_all(epoch: int) -> Counter:
    outcomes: Counter = Counter()
    while records := take_pending(epoch):
        for record in records:
            outcome = resume_one(record, epoch, STAGGER if outcomes['resumed'] else 0)
            logger.info('%s: %s', record['session_id'][:8], outcome)
            outcomes[outcome] += 1
    return outcomes


def launch(record: dict, epoch: int) -> str:
    binary = claude_binary()
    if binary is None:
        return 'no-claude'
    if not allow_resume(record['session_id'], datetime.now(timezone.utc)):
        return 'gave-up'
    job = {'binary': binary, 'session_id': record['session_id'], 'cwd': record['cwd'], 'epoch': epoch}
    try:
        start_supervisor(job)
    except OSError as e:
        logger.info('could not start %s: %s', record['session_id'][:8], type(e).__name__)
        return 'failed'
    return 'resumed'


def count(n: int, noun: str) -> str:
    return f'{n} {noun}' if n == 1 else f'{n} {noun}s'


def summary(outcomes: Counter) -> str | None:
    """One line saying what Goon did, or None when every session had already been continued."""
    parts = []
    if outcomes['resumed']:
        parts.append(f'resumed {count(outcomes["resumed"], "closed session")}')
    if outcomes['open']:
        parts.append(f'{count(outcomes["open"], "open session")} left to the Goon mod inside')
    skipped = sum(n for label, n in outcomes.items() if label not in ('resumed', 'open', 'continued', 'off'))
    if skipped:
        parts.append(f'{skipped} skipped, see the log')
    return f'Limit reset: {", ".join(parts)}.' if parts else None


def is_permanent(error: Exception) -> bool:
    """A bad URL or header, or a 4xx other than 429, fails the same way every time."""
    if isinstance(error, urllib.error.HTTPError):
        return 400 <= error.code < 500 and error.code != 429
    return isinstance(error, (ValueError, http.client.InvalidURL))


def describe(error: Exception) -> str:
    """Class (and HTTP status) only: an error message can quote the topic or the token."""
    return f'HTTP {error.code}' if isinstance(error, urllib.error.HTTPError) else type(error).__name__


def push(text: str, settings: dict[str, str]) -> None:
    if not settings['ntfy_topic']:
        return
    headers = {'Title': 'Claude Goon', 'Tags': 'arrow_forward'}
    if settings['notify_email']:
        headers['Email'] = settings['notify_email']
    if settings['ntfy_token']:
        headers['Authorization'] = f'Bearer {settings["ntfy_token"]}'  # ntfy.sh refuses anonymous email
    request = urllib.request.Request(
        NTFY_URL + urllib.parse.quote(settings['ntfy_topic'], safe=''), data=text.encode(), headers=headers
    )
    for attempt in range(1, 4):
        try:
            urllib.request.urlopen(request, timeout=15).read()
            logger.info('pushed (attempt %d)', attempt)
            return
        except (OSError, ValueError, http.client.HTTPException) as e:
            logger.info('push failed (attempt %d): %s', attempt, describe(e))
            if is_permanent(e):
                break
            time.sleep(30)
    logger.info('gave up pushing')


def lookup(path: str | None, tries: int) -> Limit | None:
    if not path:
        return None
    for attempt in range(tries):
        limit = find_limit(Path(path), datetime.now(timezone.utc))
        if limit or attempt == tries - 1:
            return limit
        time.sleep(1)
    return None


def watch(payload: dict) -> None:
    limit = lookup(payload.get('transcript_path'), LOOKUP_TRIES)
    if limit is None:
        logger.info('no limit message in transcript (error=%r)', payload.get('error'))
        return
    if limit.reset - datetime.now(timezone.utc) > MAX_WAIT:
        logger.info('unusable reset time %s', limit.reset)
        return
    epoch = int(limit.reset.timestamp())
    register(epoch, payload)
    marker = DIR / f'{epoch}.lock'
    fd = claim(marker)
    if fd is None:
        logger.info('already watching the %s reset', limit.reset.strftime('%Y-%m-%d %H:%M'))
        return
    settings = read_settings()
    if settings['quiet_hours'] and parse_quiet(settings['quiet_hours']) is None:
        logger.info('ignoring quiet hours %r: not HH:MM-HH:MM', settings['quiet_hours'])
    resume_at = after_quiet(limit.reset + MARGIN, settings['quiet_hours'])
    try:
        logger.info(
            '%s limit; will resume at %s',
            limit.window,
            resume_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'),
        )
        sleep_until(resume_at)
        outcomes = resume_all(epoch)
    finally:
        release(fd, marker)
    if text := summary(outcomes):
        push(text, settings)


def session_transcript(session_id: str) -> str | None:
    found = next((CLAUDE_DIR / 'projects').glob(f'*/{session_id}.jsonl'), None)
    return str(found) if found else None


def print_reset(session_id: str, quiet_hours: str) -> None:
    """Print the session's latest limit error as JSON, with when to resume it; nothing when it has none."""
    limit = lookup(session_transcript(session_id), LOOKUP_TRIES)
    if limit:
        resume_at = after_quiet(limit.reset + MARGIN, quiet_hours)
        reply = {
            'resetAt': int(limit.reset.timestamp()),
            'resumeAt': int(resume_at.timestamp()),
            'window': limit.window,
        }
        print(json.dumps(reply))


def spawn_detached(mode: str, payload: bytes) -> None:
    child = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    child.stdin.write(payload)
    child.stdin.close()


def start_watcher(raw: bytes) -> None:
    spawn_detached('watch', raw)


def start_supervisor(job: dict) -> None:
    spawn_detached('run', json.dumps(job).encode())


def run_job(job: dict) -> None:
    code = supervise(job, read_settings())
    logger.info('%s: claude exited %d', job['session_id'][:8], code)


def configure_logging() -> None:
    log_path = DIR / 'log.txt'
    os.close(os.open(log_path, os.O_CREAT, 0o600))
    handler = logging.FileHandler(log_path)
    formatter = logging.Formatter('%(asctime)sZ %(message)s', '%Y-%m-%d %H:%M:%S')
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', nargs='?', choices=['watch', 'reset', 'run', 'power'])
    parser.add_argument('word', nargs='?', choices=['on', 'off', 'status'], default='status')
    parser.add_argument('--session')
    parser.add_argument('--quiet', default='')
    args = parser.parse_args(argv)
    if args.mode == 'reset' and not args.session:
        parser.error('reset needs --session')
    DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    configure_logging()
    if args.mode == 'power':
        print(power(args.word))
        sys.exit(0 if is_enabled() else OFF_EXIT)  # the in-session mod reads the switch from this
    if args.mode == 'reset':
        print_reset(args.session, args.quiet)
    elif args.mode in ('watch', 'run'):
        try:
            (watch if args.mode == 'watch' else run_job)(json.load(sys.stdin))
        except Exception:
            logger.exception('%s failed', args.mode)
    else:
        raw = sys.stdin.buffer.read()
        if not json.loads(raw or b'{}').get('agent_id'):  # a subagent's stop: its session's own turn reports the limit
            start_watcher(raw)


if __name__ == '__main__':
    main(sys.argv[1:])
