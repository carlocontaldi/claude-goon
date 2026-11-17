"""What Goon sends a resumed session: the presets, the default message and each session's own choice."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

DEFAULT_MESSAGE = 'Go on'
BUILT_IN_PRESETS = (
    DEFAULT_MESSAGE,
    'Go on and review',
    'Go on and recap',
    'Go on and wrap up',
    'Go on, then review and recap',
    'Go on, then review and wrap up',
)
MAX_MESSAGE = 1000
SESSION_ID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


class ChoiceError(ValueError):
    """A request the person can fix; the text says how."""


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    scratch = path.with_suffix('.tmp')
    with os.fdopen(os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
        json.dump(data, f)
    scratch.replace(path)


def clean(text: str) -> str:
    """The message on one line; raise ChoiceError if it is empty or too long."""
    text = ' '.join(text.split())
    if not text:
        raise ChoiceError('The message is empty.')
    if len(text) > MAX_MESSAGE:
        raise ChoiceError(f'The message is {len(text)} characters; the limit is {MAX_MESSAGE}.')
    return text


def user_presets(folder: Path) -> list[str]:
    saved = read_json(folder / 'presets.json')
    return [p for p in saved if isinstance(p, str)] if isinstance(saved, list) else []


def presets(folder: Path) -> list[str]:
    """The built-in presets, then the ones the person added."""
    return [*BUILT_IN_PRESETS, *(p for p in user_presets(folder) if p not in BUILT_IN_PRESETS)]


def resolve(folder: Path, spec: str) -> str:
    """The message a preset's number (as `presets` lists it) or free text stands for."""
    spec = clean(spec)
    if not spec.isdigit():
        return spec
    options = presets(folder)
    if not 1 <= int(spec) <= len(options):
        raise ChoiceError(f'There is no preset {spec}; /goon presets lists them.')
    return options[int(spec) - 1]


def add_preset(folder: Path, text: str) -> str:
    text = clean(text)
    if text in presets(folder):
        raise ChoiceError(f'"{text}" is already a preset.')
    write_json(folder / 'presets.json', [*user_presets(folder), text])
    return text


def remove_preset(folder: Path, spec: str) -> str:
    """Remove an added preset named by its number or its text and return its text."""
    text = resolve(folder, spec)
    if text in BUILT_IN_PRESETS:
        raise ChoiceError(f'"{text}" is built in and cannot be removed.')
    added = user_presets(folder)
    if text not in added:
        raise ChoiceError(f'"{text}" is not a preset.')
    write_json(folder / 'presets.json', [p for p in added if p != text])
    return text


def default_message(folder: Path, option: str) -> str:
    """The message set with `/goon message`, else the resume_text option, else the built-in default."""
    saved = read_json(folder / 'message.json')
    chosen = saved.get('message') if isinstance(saved, dict) else None
    return chosen if isinstance(chosen, str) and chosen else option or DEFAULT_MESSAGE


def set_default_message(folder: Path, text: str | None) -> None:
    if text is None:
        (folder / 'message.json').unlink(missing_ok=True)
    else:
        write_json(folder / 'message.json', {'message': text})


def session_file(folder: Path, session_id: str) -> Path:
    if not SESSION_ID.fullmatch(session_id):
        raise ChoiceError(f'{session_id!r} is not a session id.')
    return folder / 'sessions' / f'{session_id}.json'


def session_choice(folder: Path, session_id: str) -> dict:
    """What the person chose for this interruption of the session: `enabled` False and/or a `message`."""
    if not SESSION_ID.fullmatch(session_id):
        return {}
    saved = read_json(session_file(folder, session_id))
    return saved if isinstance(saved, dict) else {}


def choose(folder: Path, session_id: str, **changes: object) -> None:
    """Set the given choices for the session; a value of None drops that choice."""
    path = session_file(folder, session_id)
    choice = {**session_choice(folder, session_id), **changes}
    choice = {key: value for key, value in choice.items() if value is not None}
    if choice:
        write_json(path, choice)
    else:
        path.unlink(missing_ok=True)


def forget(folder: Path, session_id: str) -> None:
    if SESSION_ID.fullmatch(session_id):
        session_file(folder, session_id).unlink(missing_ok=True)


def is_enabled_for(folder: Path, session_id: str) -> bool:
    return session_choice(folder, session_id).get('enabled') is not False


def message_for(folder: Path, session_id: str, option: str) -> str:
    """The session's own message, else the default one."""
    own = session_choice(folder, session_id).get('message')
    return own if isinstance(own, str) and own else default_message(folder, option)
