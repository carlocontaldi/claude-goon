"""A fake API answers `claude` with a 429: the plugin's module hook must run its script with the hook input.
The script is a stub that records its arguments and stdin. Usage: python3 tests/e2e_stopfailure.py"""

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


def main() -> int:
    claude = shutil.which('claude')
    if not claude:
        print('no claude on the PATH')
        return 2
    server = http.server.HTTPServer(('127.0.0.1', 0), RateLimited)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with tempfile.TemporaryDirectory() as tmp:
        stub = Path(tmp, 'bin', 'python3')
        stub.parent.mkdir()
        stub.write_text(STUB)
        stub.chmod(0o755)
        env = {
            **os.environ,
            'PATH': f'{stub.parent}{os.pathsep}{os.environ["PATH"]}',
            'STUB_OUT': str(Path(tmp, 'call')),
            'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{server.server_port}',
            'ANTHROPIC_AUTH_TOKEN': 'x',
            'CLAUDE_CONFIG_DIR': str(Path(tmp, 'config')),
        }
        command = [claude, '-p', 'hi', '--plugin-dir', str(PLUGIN), '--output-format', 'json']
        subprocess.run(command, env=env, cwd=tmp, stdin=subprocess.DEVNULL, capture_output=True, timeout=120)
        for stdin_file in glob.glob(str(Path(tmp, 'call.*.stdin'))):
            hook_input = Path(stdin_file).read_text()
            args = Path(stdin_file.replace('.stdin', '.args')).read_text().splitlines()
            if '"error":"rate_limit"' in hook_input and args and args[0].endswith('.py'):
                print(f'the hook ran {Path(args[0]).name} with the hook input')
                return 0
    print('the hook did not run its script with the hook input')
    return 1


if __name__ == '__main__':
    sys.exit(main())
