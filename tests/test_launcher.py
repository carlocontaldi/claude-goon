import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1] / 'hooks'
SCRIPT = 'goon.py'


def commands(node):
    """Every hook command line in hooks.json."""
    if isinstance(node, dict):
        if 'command' in node:
            yield node['command']
        node = list(node.values())
    if isinstance(node, list):
        for child in node:
            yield from commands(child)


@unittest.skipIf(os.name == 'nt', 'the hook command is a POSIX shell line')
class LauncherTest(unittest.TestCase):
    """The hook runs `python3`, or `python` when `python3` is missing or does not run."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bin = Path(tmp.name, 'bin')
        self.bin.mkdir()
        self.plugin = Path(tmp.name, 'plugin')
        (self.plugin / 'hooks').mkdir(parents=True)
        (self.plugin / 'hooks' / SCRIPT).write_text("import os, sys\nprint(os.environ['WHICH'], sys.stdin.read())\n")
        [self.command] = commands(json.loads((HOOKS / 'hooks.json').read_text())['hooks'])

    def install(self, name, works=True):
        """A stand-in for the interpreter `name`; a broken one exits at once, as the Windows Store stub does."""
        stub = self.bin / name
        stub.write_text(f'#!/bin/sh\nWHICH={name} exec "{sys.executable}" "$@"\n' if works else '#!/bin/sh\nexit 9\n')
        stub.chmod(0o755)

    def run_hook(self):
        env = {'PATH': str(self.bin), 'CLAUDE_PLUGIN_ROOT': str(self.plugin)}
        return subprocess.run(['/bin/sh', '-c', self.command], input='payload', capture_output=True, text=True, env=env)

    def test_python3_is_used_when_it_runs(self):
        self.install('python3')
        self.install('python')
        self.assertEqual(self.run_hook().stdout, 'python3 payload\n')

    def test_python_is_used_when_python3_is_missing(self):
        self.install('python')
        self.assertEqual(self.run_hook().stdout, 'python payload\n')

    def test_python_is_used_when_python3_does_not_run(self):
        self.install('python3', works=False)
        self.install('python')
        self.assertEqual(self.run_hook().stdout, 'python payload\n')

    def test_the_hook_fails_loudly_when_neither_runs(self):
        self.install('python3', works=False)
        done = self.run_hook()
        self.assertEqual(done.returncode, 1)
        self.assertIn('no working python3 or python', done.stderr)
