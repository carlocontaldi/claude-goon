"""A fake API answers `claude` with a 429: each of the plugin's two StopFailure hooks, alone, must run its script
with the hook input, and both together must hand it inputs the script treats as one delivery.
The script is a stub that records its arguments and stdin.
Usage: python3 tests/e2e_stopfailure.py [--real-login]; the flag runs under your own Claude login, where an
organization's built-in security plugin skips the module hook, so only the command hook is checked."""

import glob
import http.server
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
SCRIPT = 'goon.py'
STUB = """#!/bin/sh
if [ "$1" = "-c" ]; then exit 0; fi
printf '%s\\n' "$@" > "$STUB_OUT.$$.args"
cat > "$STUB_OUT.$$.stdin"
"""


class RateLimited(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        body = json.dumps({'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'rate limit'}}).encode()
        self.send_response(429)
        for name, value in (('Content-Type', 'application/json'), ('retry-after', '0'), ('x-should-retry', 'false')):
            self.send_header(name, value)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def plugin_copy(tmp: str, name: str, drop: str) -> Path:
    """A copy of the plugin whose hooks.json lacks the `drop` key: "modules" or "hooks"; any other value drops none."""
    copy = Path(tmp, name)
    shutil.copytree(PLUGIN, copy, ignore=shutil.ignore_patterns('.git', 'tests', '__pycache__', '*_cache'))
    hooks_json = copy / 'hooks' / 'hooks.json'
    manifest = json.loads(hooks_json.read_text())
    manifest.pop(drop, None)
    hooks_json.write_text(json.dumps(manifest))
    return copy


def deliveries(claude: str, plugin: Path, port: int, tmp: str, real_login: bool) -> list[dict]:
    """The hook inputs the stubbed script got when `claude` ran `plugin` against the fake API."""
    out = Path(tmp, f'{plugin.name}-calls')
    out.mkdir()
    stub = Path(tmp, f'{plugin.name}-bin', 'python3')
    stub.parent.mkdir()
    stub.write_text(STUB)
    stub.chmod(0o755)
    env = {
        **os.environ,
        'PATH': f'{stub.parent}{os.pathsep}{os.environ["PATH"]}',
        'STUB_OUT': str(out / 'call'),
        'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{port}',
    }
    if not real_login:
        env.update(ANTHROPIC_AUTH_TOKEN='x', CLAUDE_CONFIG_DIR=str(Path(tmp, f'{plugin.name}-config')))
    command = [claude, '-p', 'hi', '--plugin-dir', str(plugin), '--output-format', 'json']
    subprocess.run(command, env=env, cwd=tmp, stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
    inputs = []
    for stdin_file in glob.glob(str(out / 'call.*.stdin')):
        args = Path(stdin_file.replace('.stdin', '.args')).read_text().splitlines()
        if (
            len(args) == 1 and Path(args[0]).resolve() == (plugin / 'hooks' / SCRIPT).resolve()
        ):  # the hook, not a command
            inputs.append(json.loads(Path(stdin_file).read_text()))
    return [hook_input for hook_input in inputs if hook_input.get('error') == 'rate_limit']


def main() -> int:
    claude = shutil.which('claude')
    if not claude:
        print('no claude on the PATH')
        return 2
    real_login = '--real-login' in sys.argv
    server = http.server.HTTPServer(('127.0.0.1', 0), RateLimited)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sys.path.insert(0, str(PLUGIN / 'hooks'))
    from limitwatch import DELIVERY_FIELDS

    variants = [('command hook', 'modules', 1)]
    if not real_login:  # under an organization's login the module hook is skipped, so only the command hook runs
        variants += [('module hook', 'hooks', 1), ('both hooks', '', 2)]
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for label, drop, expected in variants:
            plugin = plugin_copy(tmp, label.replace(' ', '-'), drop)
            inputs = deliveries(claude, plugin, server.server_port, tmp, real_login)
            identities = {json.dumps([hook_input.get(field) for field in DELIVERY_FIELDS]) for hook_input in inputs}
            ok = len(inputs) == expected and len(identities) == 1
            print(f'{label}: {len(inputs)} input(s), {len(identities)} distinct delivery: {"ok" if ok else "FAILED"}')
            failed += not ok
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
