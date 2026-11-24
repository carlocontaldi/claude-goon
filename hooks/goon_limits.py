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


def one_line(value: object, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    text = ' '.join(value.split())
    return text if len(text) <= limit else text[: limit - 1] + '…'


def session_info(path: Path) -> dict:
    """Title, last prompt, branch, model, cost and size of a session, as far as its transcript's tail says."""
    facts: dict = {}
    for row in tail_rows(path):  # oldest first, so the newest of each fact wins
        kind = row.get('type')
        if kind == 'ai-title':
            facts['title'] = one_line(row.get('aiTitle'), 100)
        elif kind == 'last-prompt':
            facts['last_prompt'] = one_line(row.get('lastPrompt'), 200)
        elif kind == 'cost-state' and isinstance(row.get('totalCostUSD'), (int, float)):
            facts['cost'] = row['totalCostUSD']
        elif kind == 'assistant' and not row.get('isSidechain'):
            message = row.get('message')
            model = message.get('model') if isinstance(message, dict) else None
            if isinstance(model, str) and not model.startswith('<'):  # '<synthetic>' marks Claude Code's own rows
                facts['model'] = model
        if not row.get('isSidechain'):
            facts['branch'] = one_line(row.get('gitBranch'), 100) or facts.get('branch')
            facts['cwd'] = one_line(row.get('cwd'), 500) or facts.get('cwd')
    try:
        facts['size'] = path.stat().st_size
    except OSError:
        pass
    return {key: value for key, value in facts.items() if value is not None}


# A Windows file time counts 100 ns from 1601; the registry's `startedAt` counts ms from 1970.
FILETIME_AT_EPOCH = 116444736000000000
REGISTRY_LAG_MS = 60_000


def started_after(created_ms: float, session_started_ms: float) -> bool:
    """Whether a process was created after the session began, beyond the lag of writing the registry: a reused pid."""
    return created_ms > session_started_ms + REGISTRY_LAG_MS


def is_running_on_windows(pid: int, session_started_ms: object) -> bool:
    """Whether `pid` is a live process created no later than the session began."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
            return False
        if not isinstance(session_started_ms, (int, float)):
            return True
        created, unused = wintypes.FILETIME(), wintypes.FILETIME()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), *[ctypes.byref(unused)] * 3):
            return True
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return not started_after((ticks - FILETIME_AT_EPOCH) / 10_000, session_started_ms)
    finally:
        kernel32.CloseHandle(handle)


def is_running(info: dict) -> bool:
    """Whether the registry entry's process is alive and still the one that started: a reused pid is not."""
    pid = int(info['pid'])
    if os.name == 'nt':
        return is_running_on_windows(pid, info.get('startedAt'))
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
