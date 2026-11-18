#!/usr/bin/env python3
"""StopFailure hook: resume sessions a usage limit interrupted, once it resets (`reset` serves the in-session mod)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from goon_choices import (
    SESSION_ID,
    ChoiceError,
    add_preset,
    choose,
    default_message,
    forget,
    is_enabled_for,
    message_for,
    presets,
    read_json,
    remove_preset,
    resolve,
    session_choice,
    set_default_message,
    write_json,
)
from goon_limits import is_blocked, is_open, session_info
from goon_quiet import after_quiet, parse_quiet
from limitwatch import (
    MARGIN,
    Offer,
    announcement,
    claim,
    effective_topic,
    format_reset,
    lookup,
    release,
    sleep_until,
    stored_topic,
    subscribe_steps,
    try_open,
)
from limitwatch import push as ntfy_push

logger = logging.getLogger('goon')

CLAUDE_DIR = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude')
DIR = Path(os.environ.get('GOON_DIR') or CLAUDE_DIR / 'goon')
NTFY_URL = (os.environ.get('GOON_NTFY_URL') or 'https://ntfy.sh').rstrip('/') + '/'
STAGGER = 15  # seconds between launches: each resume re-reads a whole conversation
STALE_NOTE = 24 * 3600  # seconds after its resume time that a note of the mod's is still listed
MAX_RESUMES = 4  # per session and day, so a session that keeps hitting a limit is not looped forever
LOOKUP_TRIES = 10  # the limit error may not be in the transcript yet when the hook fires
TOPIC_PREFIX = 'claude-goon'
ICON_URL = 'https://raw.githubusercontent.com/carlocontaldi/claude-goon/main/assets/goon-icon-64.png'
PROMPT_WAIT = 24 * 3600  # seconds a resumed session may sit on a permission prompt before its run is ended
PERMISSION_MODES = ('acceptEdits', 'auto', 'dontAsk', 'plan', 'manual')  # never bypassPermissions
OFF_EXIT = 3  # `power` exit code while off; 1 and 2 are a crash and a usage error. register.ts holds a copy


def register(epoch: int, payload: dict, facts: dict | None = None) -> None:
    """Record the interrupted session, with what is known about it, under its reset time for that reset's watcher."""
    folder = DIR / 'pending' / str(epoch)
    record = {**(facts or {})}
    record.update(session_id=payload['session_id'], cwd=payload['cwd'], transcript_path=payload['transcript_path'])
    target = folder / f'{payload["session_id"]}.json'
    scratch = target.with_suffix('.tmp')
    for _ in range(2):  # a finishing watcher may remove the empty folder between our mkdir and our write
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            scratch.write_text(json.dumps(record))
            break
        except FileNotFoundError:
            continue
    scratch.replace(target)


def has_pending(epoch: int) -> bool:
    return any((DIR / 'pending' / str(epoch)).glob('*.json'))


def take_pending(epoch: int) -> list[dict]:
    """Remove and return the sessions registered for this reset; an unreadable record is dropped and logged."""
    folder = DIR / 'pending' / str(epoch)
    records = []
    for entry in sorted(folder.glob('*.json')):
        try:
            records.append(json.loads(entry.read_text()))
        except ValueError:
            logger.info('dropping an unreadable pending record %s', entry.name[:8])
        entry.unlink()
    try:
        folder.rmdir()
    except OSError:
        pass  # missing, or a session registered meanwhile
    return records


def armed_notes(drop_stale: bool = False) -> list[dict]:
    """The notes the mod left for the sessions it armed, minus those whose resume time is long past."""
    fresh = []
    for entry in (DIR / 'armed').glob('*.json'):
        note = read_json(entry)
        valid = isinstance(note, dict) and isinstance(note.get('session_id'), str)
        if valid and isinstance(note.get('resume_at'), int) and note['resume_at'] + STALE_NOTE > time.time():
            fresh.append(note)
        elif drop_stale:
            entry.unlink(missing_ok=True)
    return fresh


def pending_records() -> list[tuple[int, dict]]:
    """Every session Goon waits for, with its reset, the soonest first: the hook's, then the mod's alone."""
    found = []
    for folder in (DIR / 'pending').glob('*'):
        if not folder.name.isdigit():
            continue
        for entry in folder.glob('*.json'):
            record = read_json(entry)
            if isinstance(record, dict) and isinstance(record.get('session_id'), str):
                found.append((int(folder.name), record))
    registered = {record['session_id'] for _, record in found}
    for note in armed_notes():
        if note['session_id'] not in registered:
            found.append((note.get('reset', note['resume_at']), {**note, 'source': 'mod'}))
    return sorted(found, key=lambda item: (item[0], item[1]['session_id']))


def find_session(prefix: str) -> str:
    """The id of the pending session that `prefix` (four characters or more) starts; a whole id is taken as is."""
    word = prefix.strip().lower()
    if SESSION_ID.fullmatch(word):
        return word
    if len(word) < 4:
        raise ChoiceError('Name a session by the first four or more characters of its id, as /goon list shows.')
    ids = {record['session_id'] for _, record in pending_records() if record['session_id'].startswith(word)}
    if not ids:
        raise ChoiceError(f'No pending session starts with {word}; /goon list shows them.')
    if len(ids) > 1:
        raise ChoiceError(f'{count(len(ids), "pending session")} start with {word}; type more of the id.')
    return ids.pop()


def session_label(record: dict) -> str:
    return record.get('title') or Path(record.get('cwd') or '').name or record['session_id'][:8]


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


def option(name: str) -> str:
    return os.environ.get(f'CLAUDE_PLUGIN_OPTION_{name.upper()}', '').strip()


def read_settings() -> dict[str, str]:
    names = (
        'resume_text',
        'resume_permission_mode',
        'quiet_hours',
        'ntfy_topic',
        'notify_email',
        'ntfy_token',
        'push_details',
    )
    settings = {n: option(n) for n in names}
    settings['ntfy_topic'] = effective_topic(settings['ntfy_topic'], DIR / 'topic', TOPIC_PREFIX)
    return settings


def wants_details(settings: dict[str, str]) -> bool:
    return settings['push_details'].lower() in ('true', '1', 'yes', 'on')


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
    text = job.get('message') or message_for(DIR, job['session_id'], settings['resume_text'])
    process.stdin.write(user_message(text))
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
                who = f'"{job["label"]}"' if job.get('label') and wants_details(settings) else 'A resumed session'
                push(f'{who} is waiting on a permission prompt for {tool}.', settings)
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
        return 'open'  # the mod inside reads this session's choices itself
    if not is_enabled_for(DIR, record['session_id']):
        return 'session-off'
    if not Path(record['cwd']).is_dir():
        return 'no-cwd'
    return 'resume'


def resume_one(record: dict, epoch: int, pause: int) -> str:
    """Return the outcome for one pending session; an error in it is logged and becomes 'failed'."""
    try:
        outcome = decide(record)
        if outcome == 'resume' and pause:
            time.sleep(pause)
            outcome = decide(record)  # the person may have opened the session while it waited its turn
        if outcome != 'resume':
            return outcome
        return launch(record, epoch)
    except Exception:
        logger.exception('%s: failed', record['session_id'][:8])
        return 'failed'


def resume_all(epoch: int) -> list[tuple[dict, str]]:
    """Handle every session registered for the reset; each record comes back with its outcome."""
    results: list[tuple[dict, str]] = []
    while records := take_pending(epoch):
        for record in records:
            started = sum(1 for _, outcome in results if outcome == 'resumed')
            outcome = resume_one(record, epoch, STAGGER if started else 0)
            logger.info('%s: %s', record['session_id'][:8], outcome)
            if outcome != 'open':  # an open session's mod reads its choices when it fires, then drops them
                forget(DIR, record['session_id'])
            results.append((record, outcome))
    return results


def launch(record: dict, epoch: int) -> str:
    binary = claude_binary()
    if binary is None:
        return 'no-claude'
    if not allow_resume(record['session_id'], datetime.now(timezone.utc)):
        return 'gave-up'
    message = message_for(DIR, record['session_id'], option('resume_text'))
    job = {
        'binary': binary,
        'session_id': record['session_id'],
        'cwd': record['cwd'],
        'epoch': epoch,
        'message': message,
        'label': session_label(record),
    }
    try:
        start_supervisor(job)
    except OSError as e:
        logger.info('could not start %s: %s', record['session_id'][:8], type(e).__name__)
        return 'failed'
    return 'resumed'


def count(n: int, noun: str) -> str:
    return f'{n} {noun}' if n == 1 else f'{n} {noun}s'


def summary_parts(outcomes: Counter) -> list[str]:
    """What Goon did with the sessions, as phrases; empty when every session had already continued."""
    parts = []
    if outcomes['resumed']:
        parts.append(f'resumed {count(outcomes["resumed"], "closed session")}')
    if outcomes['open']:
        parts.append(f'{count(outcomes["open"], "open session")} left to the Goon mod inside')
    quiet = ('resumed', 'open', 'continued', 'off', 'session-off')
    skipped = sum(n for outcome, n in outcomes.items() if outcome not in quiet)
    if skipped:
        parts.append(f'{skipped} skipped, see the log')
    return parts


def summary(outcomes: Counter, label: str) -> str | None:
    """One line saying which limit reset and what Goon did, or None when every session had already continued."""
    parts = summary_parts(outcomes)
    return f'{label[0].upper()}{label[1:]} reset: {", ".join(parts)}.' if parts else None


def merged_text(restored: list, pending: list, outcomes: Counter) -> str:
    """Portavoce's announcement, then what Goon did: the limit that reset is named once."""
    return f'{announcement(restored, pending)} Goon: {", ".join(summary_parts(outcomes))}.'


OUTCOME_PHRASES = {
    'resumed': 'resumed',
    'open': 'left to the Goon mod inside',
    'session-off': 'left alone, switched off for this session',
    'no-cwd': 'skipped, its folder is gone',
    'gave-up': f'skipped, already resumed {MAX_RESUMES} times today',
    'unknown': 'skipped, could not tell whether it is open',
    'unreadable': 'skipped, its transcript is unreadable',
    'no-claude': 'skipped, claude was not found',
    'failed': 'failed, see the log',
}
MAX_DETAIL_LINES = 8


def session_lines(results: list[tuple[dict, str]]) -> str:
    """One line per session Goon acted on or skipped, named by its title or folder, for a push that names them."""
    lines = [
        f'- {session_label(record)}: {OUTCOME_PHRASES[outcome]}'
        for record, outcome in results
        if outcome in OUTCOME_PHRASES
    ]
    if len(lines) > MAX_DETAIL_LINES:
        lines = [*lines[:MAX_DETAIL_LINES], f'and {len(lines) - MAX_DETAIL_LINES} more']
    return '\n'.join(lines)


def push(text: str, settings: dict[str, str], merged: bool = False, with_email: bool = True) -> bool:
    return ntfy_push(
        NTFY_URL,
        settings['ntfy_topic'],
        text,
        logger.info,
        title='Claude limit restored' if merged else 'Claude Goon',
        tags='parrot,service_dog' if merged else 'service_dog,arrow_forward',
        icon=ICON_URL,
        email=settings['notify_email'] if with_email else '',
        token=settings['ntfy_token'],
    )


def topic_text(rotate: bool = False) -> str:
    """What `/goon topic` says: the topic pushes go to and how to subscribe the phone to it."""
    option = os.environ.get('CLAUDE_PLUGIN_OPTION_NTFY_TOPIC', '').strip()
    if option:
        note = 'The topic option is set, so that is the topic; clear the option to use a private one made here.'
        return f'Goon pushes to {option}. {note} {subscribe_steps(option, "/goon test")}'
    topic = stored_topic(DIR / 'topic', TOPIC_PREFIX, rotate=rotate)
    made = 'Made a new private topic, so drop the old one in the app. ' if rotate else ''
    return f'{made}Goon pushes to your private ntfy topic {topic}. {subscribe_steps(topic, "/goon test")}'


def send_test(settings: dict[str, str]) -> str:
    """Sends one push to the topic, without the email, and says whether it went through."""
    sent = ntfy_push(
        NTFY_URL,
        settings['ntfy_topic'],
        'Claude Goon test: if you can read this, the topic works.',
        logger.info,
        title='Claude Goon test',
        tags='service_dog,bell',
        icon=ICON_URL,
        token=settings['ntfy_token'],
    )
    return 'Sent: it should show up in the ntfy app now.' if sent else f'Not sent; {DIR / "log.txt"} says why.'


def track(session_id: str, resume_at: int) -> None:
    """Note a session the mod armed, so `/goon list` shows it even if the StopFailure hook never registered it."""
    path = session_transcript(session_id) if SESSION_ID.fullmatch(session_id) else None
    if path is None:
        return
    limit = lookup(path, 1)
    note = {**session_info(Path(path)), 'session_id': session_id, 'transcript_path': path, 'resume_at': resume_at}
    note['reset'] = int(limit.reset.timestamp()) if limit else resume_at
    if limit:
        note.update(limit=limit.caps[0].label, interrupted=limit.at.isoformat())
    armed_notes(drop_stale=True)
    write_json(DIR / 'armed' / f'{session_id}.json', note)


def plan(session_id: str) -> dict | None:
    """What to send the session now and the presets to pick from; None while Goon or the session is off."""
    if not is_enabled() or not is_enabled_for(DIR, session_id):
        return None
    return {'message': message_for(DIR, session_id, option('resume_text')), 'presets': presets(DIR)}


def describe(epoch: int, record: dict) -> str:
    """The `/goon list` entry of one pending session."""
    session_id = record['session_id']
    opened = is_open(session_id, CLAUDE_DIR / 'sessions')
    state = {True: 'open', False: 'closed', None: 'open or closed, unknown'}[opened]
    facts = [state, record.get('cwd', 'unknown folder') + (f' ({record["branch"]})' if record.get('branch') else '')]
    if record.get('size'):
        facts.append(f'{record["size"] / 1e6:.1f} MB' if record['size'] >= 100_000 else f'{record["size"] // 1000} KB')
    if record.get('cost') is not None:
        facts.append(f'${record["cost"]:.2f} so far')
    if record.get('model'):
        facts.append(record['model'])
    reset = format_reset(datetime.fromtimestamp(epoch, timezone.utc))
    lines = [
        f'{session_id[:8]}  {session_label(record)}',
        f'  {", ".join(facts)}',
        f'  {record.get("limit", "usage limit")}, resets {reset}',
    ]
    if record.get('source') == 'mod':
        when = format_reset(datetime.fromtimestamp(record['resume_at'], timezone.utc))
        if opened:
            lines.append(f'  only the Goon mod inside the session continues it, at {when}')
        else:
            lines.append(
                f'  no watcher has it and it is closed: nothing continues it unless you reopen it before {when}'
            )
    if is_blocked(Path(record.get('transcript_path', ''))) is False:
        lines.append('  already continued: Goon will leave it alone')
    own = session_choice(DIR, session_id)
    if own.get('enabled') is False:
        lines.append('  switched off for this session: nothing will be sent')
    else:
        text = message_for(DIR, session_id, option('resume_text'))
        lines.append(f'  will send "{text}"' + (' (chosen for this session)' if own.get('message') else ''))
    if record.get('last_prompt'):
        lines.append(f'  last prompt: {record["last_prompt"]}')
    return '\n'.join(lines)


def list_text() -> str:
    pending = pending_records()
    if not pending:
        return 'No interrupted sessions are waiting.'
    state = 'on' if is_enabled() else 'off, so nothing is resumed'
    default = default_message(DIR, option('resume_text'))
    sent = f'it sends "{default}" unless a session has its own message'
    head = f'{count(len(pending), "interrupted session")} waiting. Goon is {state}; {sent}.'
    return '\n\n'.join([head, *(describe(epoch, record) for epoch, record in pending)])


USAGE = (
    'Use /goon on, off, status, list, presets, preset add <text>, preset remove <number or text>, '
    'message [<number or text>|default], session <id> on|off|reset|message <number or text>|message default, '
    'topic, topic new or test.'
)
SESSION_USAGE = 'Use /goon session <id> on, off, reset, message <number or text> or message default.'


def command_presets(_: str) -> str:
    default = default_message(DIR, option('resume_text'))
    lines = [f'{n}. {text}' + ('  (default)' if text == default else '') for n, text in enumerate(presets(DIR), 1)]
    return '\n'.join([*lines, 'Pick one with /goon message <number>; add one with /goon preset add <text>.'])


def command_preset(rest: str) -> str:
    action, _, text = rest.partition(' ')
    if action.lower() == 'add':
        return f'Added "{add_preset(DIR, text)}".'
    if action.lower() == 'remove':
        return f'Removed "{remove_preset(DIR, text)}".'
    raise ChoiceError('Use /goon preset add <text> or /goon preset remove <number or text>.')


def command_message(rest: str) -> str:
    if rest.lower() == 'default':
        set_default_message(DIR, None)
    elif rest:
        set_default_message(DIR, resolve(DIR, rest))
    return f'Goon sends "{default_message(DIR, option("resume_text"))}" unless a session has its own message.'


def command_session(rest: str) -> str:
    prefix, _, action = rest.partition(' ')
    word, _, argument = action.strip().partition(' ')
    word = word.lower()
    if not prefix or word not in ('on', 'off', 'reset', 'message'):
        raise ChoiceError(SESSION_USAGE)
    session_id = find_session(prefix)
    name = session_id[:8]
    if word == 'reset':
        forget(DIR, session_id)
        return f'{name}: back to the defaults.'
    if word != 'message':
        choose(DIR, session_id, enabled=None if word == 'on' else False)
        return f'{name}: switched {word}.'
    if argument.strip().lower() == 'default':
        choose(DIR, session_id, message=None)
        return f'{name}: back to the default message.'
    text = resolve(DIR, argument)
    choose(DIR, session_id, message=text)
    return f'{name}: will send "{text}".'


COMMANDS = {
    'list': lambda _: list_text(),
    'presets': command_presets,
    'preset': command_preset,
    'message': command_message,
    'session': command_session,
}


def run_command(words: str) -> str:
    """Run a `/goon` command given as one string and return what to tell the person."""
    verb, _, rest = words.strip().partition(' ')
    handler = COMMANDS.get(verb.lower())
    if handler is None:
        return USAGE
    try:
        return handler(rest.strip())
    except ChoiceError as error:
        return str(error)


def watch(payload: dict) -> None:
    limit = lookup(payload.get('transcript_path'), LOOKUP_TRIES)
    if limit is None:
        logger.info('no limit message in transcript (error=%r)', payload.get('error'))
        return
    cap = limit.caps[0]
    if cap.reset - datetime.now(timezone.utc) > cap.max_wait:
        logger.info('unusable reset time %s for the %s', cap.reset, cap.label)
        return
    epoch = int(cap.reset.timestamp())
    facts = {**session_info(Path(payload['transcript_path'])), 'limit': cap.label, 'interrupted': limit.at.isoformat()}
    register(epoch, payload, facts)
    marker = DIR / f'{epoch}.lock'
    fd = claim(marker)
    if fd is None:
        logger.info('already watching the %s reset', cap.reset.strftime('%Y-%m-%d %H:%M'))
        return
    settings = read_settings()
    if settings['quiet_hours'] and parse_quiet(settings['quiet_hours']) is None:
        logger.info('ignoring quiet hours %r: not HH:MM-HH:MM', settings['quiet_hours'])
    resume_at = after_quiet(cap.reset + MARGIN, settings['quiet_hours'])
    logger.info('%s; will resume at %s', cap.label, resume_at.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S'))
    results: list[tuple[dict, str]] = []
    offer = try_open(Offer(resume_at, settings['notify_email']), logger.info) if can_email(settings) else None
    owns = None
    try:
        while fd is not None:
            try:
                sleep_until(resume_at)
                if owns is None and offer and is_enabled() and has_pending(epoch):
                    owns = claim_email(offer)  # before the resumes, so Portavoce can push to the phone at once
                results += resume_all(epoch)
            finally:
                release(fd, marker)
            # A session registered after our last look found the lock held and left it to us.
            fd = claim(marker) if has_pending(epoch) else None
        send_report(results, cap.label, settings, offer, owns)
    finally:
        if offer:
            offer.close()


def can_email(settings: dict[str, str]) -> bool:
    return bool(settings['notify_email'] and settings['ntfy_token'])


def claim_email(offer: Offer) -> bool:
    """Decide that Goon sends the email: False when Portavoce decided to send its own, True when the folder fails."""
    try:
        return offer.claim()
    except OSError as error:
        logger.info('no hand-over: %s', type(error).__name__)
        return True


def merge_note(offer: Offer) -> tuple[list, list] | None:
    """Portavoce's announcement, waited for while its watcher is known to be coming."""
    try:
        return offer.wait_note()
    except OSError as error:
        logger.info('no hand-over: %s', type(error).__name__)
        return None


def mark_sent(offer: Offer) -> None:
    try:
        offer.delivered()
    except OSError as error:
        logger.info('no hand-over: %s', type(error).__name__)


def send_report(
    results: list[tuple[dict, str]], label: str, settings: dict[str, str], offer: Offer | None, owns: bool | None
) -> None:
    """Push what Goon did, with Portavoce's announcement in front when its watcher left one for this email."""
    outcomes = Counter(outcome for _, outcome in results)
    text = summary(outcomes, label)
    if text is None:
        return  # nothing to say: Portavoce sends its own email
    if owns is None:  # Goon did not claim at the reset, but has something to say now
        owns = claim_email(offer) if offer else True
    note = merge_note(offer) if offer and owns else None
    if note:
        text = merged_text(*note, outcomes)
    if wants_details(settings) and (lines := session_lines(results)):
        text = f'{text}\n{lines}'
    if push(text, settings, merged=note is not None, with_email=owns) and offer and owns:
        mark_sent(offer)


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
    parser.add_argument(
        'mode', nargs='?', choices=['watch', 'reset', 'plan', 'track', 'run', 'power', 'topic', 'test', 'command']
    )
    parser.add_argument('word', nargs='?', choices=['on', 'off', 'status', 'new'], default='status')
    parser.add_argument('--session')
    parser.add_argument('--quiet', default='')
    parser.add_argument('--resume-at', type=int)
    free_text = argv[:1] == ['command']  # what follows is a message or an id, not options
    args = parser.parse_args(argv[:1] if free_text else argv)
    if args.mode in ('reset', 'plan', 'track') and not args.session:
        parser.error(f'{args.mode} needs --session')
    if args.mode == 'track' and args.resume_at is None:
        parser.error('track needs --resume-at')
    DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    configure_logging()
    if args.mode == 'power':
        print(power(args.word))
        sys.exit(0 if is_enabled() else OFF_EXIT)  # the in-session mod reads the switch from this
    if args.mode == 'topic':
        print(topic_text(rotate=args.word == 'new'))
    elif args.mode == 'test':
        print(send_test(read_settings()))
    elif args.mode == 'command':
        print(run_command(' '.join(argv[1:])))
    elif args.mode == 'track':
        track(args.session, args.resume_at)
    elif args.mode == 'plan':
        reply = plan(args.session)
        if reply is None:
            sys.exit(OFF_EXIT)
        print(json.dumps(reply))
    elif args.mode == 'reset':
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
