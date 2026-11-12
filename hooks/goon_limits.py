"""What a Claude Code transcript and the live-session registry say about an interrupted session."""

from __future__ import annotations

import json
import os
from pathlib import Path

from limitwatch import tail_rows


def is_blocked(path: Path) -> bool | None:
    """True while the newest message is still a limit error; None if the transcript cannot be read."""
    rows = tail_rows(path)
    if not rows:
        return None
    for row in reversed(rows):
        if row.get('type') in ('user', 'assistant') and not row.get('isSidechain'):
            return bool(row.get('isApiErrorMessage'))
    return None


def is_running(info: dict) -> bool:
    """Whether the registry entry's process is alive and still the one that started: a reused pid is not."""
    pid = int(info['pid'])
    os.kill(pid, 0)
    started = info.get('procStart')
    try:
        stat = Path(f'/proc/{pid}/stat').read_text()
        return started is None or stat.rsplit(')', 1)[1].split()[19] == str(started)
    except (OSError, IndexError):
        return True  # no /proc to compare with (not Linux): the pid being alive is all there is


def is_open(session_id: str, registry: Path) -> bool | None:
    """Whether a running Claude Code process holds the session; None if the registry cannot be read."""
    if not registry.is_dir():
        return None
    for entry in registry.glob('*.json'):
        try:
            info = json.loads(entry.read_text())
            if info.get('sessionId') == session_id and is_running(info):
                return True
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return False
