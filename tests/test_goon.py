import hashlib
import http.server
import io
import json
import os
import re
import stat
import sys
import tempfile
import threading
import unittest
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parent.parent / 'hooks'
sys.path.insert(0, str(HOOKS))
import goon  # noqa: E402
import goon_choices as choices  # noqa: E402
import goon_limits as limits  # noqa: E402
import goon_quiet as quiet  # noqa: E402
import limitwatch as lw  # noqa: E402

NOW = datetime(2026, 10, 29, 21, 40, tzinfo=timezone.utc)
SESSION = '11111111-2222-3333-4444-555555555555'
TWIN = '11111111-9999-3333-4444-555555555555'  # shares its first eight characters with SESSION
OTHER = '99999999-aaaa-bbbb-cccc-dddddddddddd'
FACTS = {
    'title': 'Fix the importer',
    'last_prompt': 'please fix the importer',
    'branch': 'main',
    'model': 'model-a',
    'cost': 1.234,
    'size': 3_400_000,
    'limit': 'weekly limit',
    'interrupted': '2026-10-29T21:40:00+00:00',
}


def stamp(at: datetime) -> str:
    return at.strftime('%Y-%m-%dT%H:%M:%S.000Z')


def limit_row(at=NOW, reset=None, text="You've hit your 5-hour limit · resets 1:40am (UTC)"):
    row = {
        'type': 'assistant',
        'timestamp': stamp(at),
        'message': {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]},
        'error': 'rate_limit',
        'isApiErrorMessage': True,
    }
    if reset is not None:
        row['apiErrorParams'] = {
            'rate_limit_info': {'status': 'rejected', 'resetsAt': reset, 'rateLimitType': 'five_hour'}
        }
    return row


def chat_row(kind='assistant', at=NOW, **extra):
    return {'type': kind, 'timestamp': stamp(at), 'message': {'role': kind, 'content': 'x'}, **extra}


class WithDirs(unittest.TestCase):
    """A scratch Goon dir, Claude dir and cwd, with the module pointed at them."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.claude = self.root / 'claude'
        (self.claude / 'sessions').mkdir(parents=True)
        self.cwd = self.root / 'project'
        self.cwd.mkdir()
        self.dir = self.root / 'goon'
        self.dir.mkdir()
        for name, value in (('DIR', self.dir), ('CLAUDE_DIR', self.claude)):
            patcher = mock.patch.object(goon, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def transcript(self, *rows):
        path = self.root / 'transcript.jsonl'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return path

    def open_session(self, session_id=SESSION, pid=None, **extra):
        entry = {'sessionId': session_id, 'pid': os.getpid() if pid is None else pid, **extra}
        (self.claude / 'sessions' / f'{entry["pid"]}.json').write_text(json.dumps(entry))

    def record(self, path, session_id=SESSION):
        return {'session_id': session_id, 'cwd': str(self.cwd), 'transcript_path': str(path)}


class IsBlockedTest(WithDirs):
    def test_a_transcript_ending_in_the_limit_error_is_blocked(self):
        self.assertTrue(limits.is_blocked(self.transcript(chat_row('user'), limit_row())))

    def test_a_later_answer_means_it_was_continued(self):
        self.assertFalse(limits.is_blocked(self.transcript(limit_row(), chat_row('user'), chat_row('assistant'))))

    def test_rows_that_are_not_messages_are_skipped(self):
        rows = [limit_row(), {'type': 'file-history-snapshot'}, chat_row('assistant', isSidechain=True)]
        self.assertTrue(limits.is_blocked(self.transcript(*rows)))

    def test_an_unreadable_transcript_is_none(self):
        self.assertIsNone(limits.is_blocked(self.root / 'nope.jsonl'))


class IsOpenTest(WithDirs):
    def test_a_session_held_by_a_running_process_is_open(self):
        self.open_session()
        self.assertTrue(limits.is_open(SESSION, self.claude / 'sessions'))

    def test_a_session_of_a_dead_process_is_not_open(self):
        self.open_session(pid=2**22 + 1)
        self.assertFalse(limits.is_open(SESSION, self.claude / 'sessions'))

    @unittest.skipUnless(Path('/proc/self/stat').exists(), 'needs /proc')
    def test_the_process_that_started_the_session_is_recognised_by_its_start_time(self):
        started = Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19]
        self.open_session(procStart=started)
        self.assertTrue(limits.is_open(SESSION, self.claude / 'sessions'))

    @unittest.skipUnless(Path('/proc/self/stat').exists(), 'needs /proc')
    def test_a_pid_reused_by_another_process_is_not_the_session(self):
        self.open_session(procStart='1')
        self.assertFalse(limits.is_open(SESSION, self.claude / 'sessions'))

    def test_another_session_does_not_count(self):
        self.open_session(session_id='other')
        self.assertFalse(limits.is_open(SESSION, self.claude / 'sessions'))

    def test_a_missing_registry_cannot_say(self):
        self.assertIsNone(limits.is_open(SESSION, self.root / 'no-registry'))


class PendingTest(WithDirs):
    def test_a_registered_session_is_taken_once(self):
        goon.register(100, self.record(Path('t.jsonl')))
        self.assertEqual([r['session_id'] for r in goon.take_pending(100)], [SESSION])
        self.assertEqual(goon.take_pending(100), [])

    def test_a_reset_nobody_registered_for_has_none(self):
        self.assertEqual(goon.take_pending(7), [])

    def test_the_folder_of_a_reset_is_removed_once_taken(self):
        goon.register(100, self.record(Path('t.jsonl')))
        goon.take_pending(100)
        self.assertFalse((self.dir / 'pending' / '100').exists())

    def test_an_unreadable_record_is_dropped_and_the_others_are_still_taken(self):
        goon.register(100, self.record(Path('t.jsonl')))
        (self.dir / 'pending' / '100' / 'bad.json').write_text('{"session_id": ')
        with self.assertLogs('goon', 'INFO') as logged:
            records = goon.take_pending(100)
        self.assertEqual([r['session_id'] for r in records], [SESSION])
        self.assertIn('dropping an unreadable pending record bad.json', '\n'.join(logged.output))
        self.assertFalse(goon.has_pending(100))

    def test_registering_survives_the_folder_being_removed_underneath(self):
        real_write = Path.write_text
        calls = []

        def write_after_a_rmdir(path, *args, **kwargs):
            if path.suffix == '.tmp' and not calls:
                calls.append(1)
                (self.dir / 'pending' / '100').rmdir()
            return real_write(path, *args, **kwargs)

        with mock.patch.object(Path, 'write_text', write_after_a_rmdir):
            goon.register(100, self.record(Path('t.jsonl')))
        self.assertTrue(goon.has_pending(100))


class AllowResumeTest(WithDirs):
    def test_a_fifth_resume_within_a_day_is_refused(self):
        self.assertEqual([goon.allow_resume(SESSION, NOW) for _ in range(5)], [True, True, True, True, False])

    def test_the_count_starts_over_after_a_day(self):
        for _ in range(4):
            goon.allow_resume(SESSION, NOW)
        self.assertTrue(goon.allow_resume(SESSION, NOW + timedelta(days=1, seconds=1)))

    def test_an_unreadable_history_starts_over_and_is_rewritten(self):
        history = self.dir / 'resumes' / f'{SESSION}.json'
        history.parent.mkdir()
        history.write_text('["2026-10-29T21:')
        self.assertTrue(goon.allow_resume(SESSION, NOW))
        self.assertEqual(json.loads(history.read_text()), [NOW.isoformat()])


class ResumeAllTest(WithDirs):
    def test_a_session_that_errors_does_not_stop_the_others(self):
        goon.register(100, self.record(Path('a.jsonl'), 'a' * 36))
        goon.register(100, self.record(Path('b.jsonl'), 'b' * 36))
        with mock.patch.object(goon, 'decide', return_value='resume'):
            with mock.patch.object(goon, 'launch', side_effect=[RuntimeError('boom'), 'resumed']):
                with self.assertLogs('goon', 'ERROR'):
                    results = goon.resume_all(100)
        self.assertEqual(Counter(outcome for _, outcome in results), Counter(failed=1, resumed=1))

    def test_a_session_opened_while_it_waited_its_turn_is_left_alone(self):
        goon.register(100, self.record(Path('a.jsonl'), 'a' * 36))
        goon.register(100, self.record(Path('b.jsonl'), 'b' * 36))
        with mock.patch.object(goon, 'STAGGER', 15), mock.patch.object(goon.time, 'sleep') as sleep:
            with mock.patch.object(goon, 'decide', side_effect=['resume', 'resume', 'open']):
                with mock.patch.object(goon, 'launch', return_value='resumed') as launch:
                    results = goon.resume_all(100)
        sleep.assert_called_once_with(15)
        launch.assert_called_once()
        self.assertEqual(Counter(outcome for _, outcome in results), Counter(resumed=1, open=1))


class ResumeArgvTest(unittest.TestCase):
    SETTINGS = {'resume_permission_mode': ''}
    BASE = ['/bin/claude', '--resume', SESSION, '-p', '--input-format', 'stream-json']

    def test_prompts_come_back_over_stream_json_instead_of_being_refused(self):
        argv = goon.resume_argv('/bin/claude', SESSION, self.SETTINGS)
        self.assertEqual(
            argv, [*self.BASE, '--output-format', 'stream-json', '--verbose', '--permission-prompt-tool', 'stdio']
        )
        self.assertNotIn('--permission-prompts', argv)

    def test_a_permission_mode_is_passed_when_set(self):
        argv = goon.resume_argv('/bin/claude', SESSION, {'resume_permission_mode': 'auto'})
        self.assertEqual(argv[-2:], ['--permission-mode', 'auto'])

    def test_bypassing_permissions_is_never_passed_on(self):
        argv = goon.resume_argv('/bin/claude', SESSION, {'resume_permission_mode': 'bypassPermissions'})
        self.assertNotIn('--permission-mode', argv)


class DecideTest(WithDirs):
    def test_a_session_someone_already_continued_is_left_alone(self):
        path = self.transcript(limit_row(), chat_row('user'))
        self.assertEqual(goon.decide(self.record(path)), 'continued')

    def test_an_open_session_is_left_to_the_mod_inside_it(self):
        self.open_session()
        self.assertEqual(goon.decide(self.record(self.transcript(limit_row()))), 'open')

    def test_a_closed_blocked_session_is_resumed(self):
        self.assertEqual(goon.decide(self.record(self.transcript(limit_row()))), 'resume')

    def test_a_closed_session_whose_directory_is_gone_is_skipped(self):
        record = {**self.record(self.transcript(limit_row())), 'cwd': str(self.root / 'gone')}
        self.assertEqual(goon.decide(record), 'no-cwd')

    def test_a_missing_registry_means_do_not_guess(self):
        (self.claude / 'sessions').rmdir()
        self.assertEqual(goon.decide(self.record(self.transcript(limit_row()))), 'unknown')

    def test_a_missing_transcript_is_skipped(self):
        self.assertEqual(goon.decide(self.record(self.root / 'nope.jsonl')), 'unreadable')


class SummaryTest(unittest.TestCase):
    def test_closed_and_open_sessions_are_counted(self):
        self.assertEqual(
            goon.summary(Counter(resumed=2, open=1), '5-hour limit'),
            '5-hour limit reset: resumed 2 closed sessions, 1 open session left to the Goon mod inside.',
        )

    def test_skipped_sessions_are_pointed_at_the_log(self):
        self.assertEqual(
            goon.summary(Counter(resumed=1, **{'gave-up': 2}), 'weekly limit'),
            'Weekly limit reset: resumed 1 closed session, 2 skipped, see the log.',
        )

    def test_the_limit_is_named_as_claude_names_it(self):
        self.assertEqual(
            goon.summary(Counter(resumed=1), 'Fable limit'), 'Fable limit reset: resumed 1 closed session.'
        )

    def test_nothing_to_say_when_everyone_continued_on_their_own(self):
        self.assertIsNone(goon.summary(Counter(continued=3), '5-hour limit'))


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class AfterQuietTest(unittest.TestCase):
    def test_a_time_outside_the_window_is_left_alone(self):
        t = utc(2026, 11, 11, 12, 0)
        self.assertEqual(quiet.after_quiet(t, '22:00-07:00 UTC'), t)

    def test_a_time_inside_a_same_day_window_moves_to_its_end(self):
        self.assertEqual(quiet.after_quiet(utc(2026, 11, 11, 10, 0), '09:00-17:00 UTC'), utc(2026, 11, 11, 17, 0))

    def test_a_window_across_midnight_ends_the_next_morning_or_this_one(self):
        self.assertEqual(quiet.after_quiet(utc(2026, 11, 11, 23, 30), '22:00-07:00 UTC'), utc(2026, 11, 12, 7, 0))
        self.assertEqual(quiet.after_quiet(utc(2026, 11, 12, 2, 0), '22:00-07:00 UTC'), utc(2026, 11, 12, 7, 0))

    def test_the_window_opens_at_its_start_and_closes_at_its_end(self):
        self.assertEqual(quiet.after_quiet(utc(2026, 11, 11, 22, 0), '22:00-07:00 UTC'), utc(2026, 11, 12, 7, 0))
        self.assertEqual(quiet.after_quiet(utc(2026, 11, 12, 7, 0), '22:00-07:00 UTC'), utc(2026, 11, 12, 7, 0))

    def test_the_zone_names_the_clock_the_window_is_read_on(self):
        summer = utc(2026, 7, 10, 21, 30)  # 22:30 in Lisbon, one hour ahead of UTC
        self.assertEqual(quiet.after_quiet(summer, '22:00-07:00 Europe/Lisbon'), utc(2026, 7, 11, 6, 0))

    def test_without_a_zone_the_machine_clock_is_used(self):
        t = datetime(2026, 11, 11, 23, 30).astimezone()
        closes = quiet.after_quiet(t, '22:00-07:00')
        self.assertEqual((closes.astimezone().hour, closes.astimezone().day), (7, 12))

    def test_no_spec_or_an_invalid_one_means_no_quiet_hours(self):
        t = utc(2026, 11, 11, 23, 30)
        for spec in ('', 'night', '22:00-22:00', '25:00-07:00', '22:00-07:00 Mars/Base'):
            self.assertEqual(quiet.after_quiet(t, spec), t, spec)


class SettingsTest(WithDirs):
    MANIFEST = json.loads((HOOKS.parent / '.claude-plugin' / 'plugin.json').read_text())['userConfig']

    def test_an_unset_or_empty_topic_option_gets_a_private_topic_that_is_kept(self):
        for value in (None, ''):
            with mock.patch.dict(os.environ):
                os.environ.pop('CLAUDE_PLUGIN_OPTION_NTFY_TOPIC', None)
                if value is not None:
                    os.environ['CLAUDE_PLUGIN_OPTION_NTFY_TOPIC'] = value
                topic = goon.read_settings()['ntfy_topic']
            self.assertRegex(topic, r'^claude-goon-[A-Za-z0-9]{22}$')
            self.assertEqual((self.dir / 'topic').read_text().strip(), topic)

    def test_a_topic_set_in_the_options_wins_and_makes_no_private_one(self):
        with mock.patch.dict(os.environ, {'CLAUDE_PLUGIN_OPTION_NTFY_TOPIC': ' mine '}):
            self.assertEqual(goon.read_settings()['ntfy_topic'], 'mine')
        self.assertFalse((self.dir / 'topic').exists())

    def test_the_resume_text_default_is_the_one_the_manifest_declares(self):
        with mock.patch.dict(os.environ):
            os.environ.pop('CLAUDE_PLUGIN_OPTION_RESUME_TEXT', None)
            self.assertEqual(goon.read_settings()['resume_text'] or choices.DEFAULT_MESSAGE, 'Go on')
        self.assertEqual(self.MANIFEST['resume_text']['default'], choices.DEFAULT_MESSAGE)

    def test_the_manifest_has_no_fixed_topic(self):
        self.assertNotIn('default', self.MANIFEST['ntfy_topic'])


class TopicCommandTest(WithDirs):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('CLAUDE_PLUGIN_OPTION_NTFY_TOPIC', None)

    def test_the_topic_is_shown_with_how_to_subscribe_and_stays_the_same(self):
        text = goon.topic_text()
        topic = (self.dir / 'topic').read_text().strip()
        self.assertIn(f'Goon pushes to your private ntfy topic {topic}.', text)
        self.assertIn('tap +', text)
        self.assertIn(f'ntfy://ntfy.sh/{topic}', text)
        self.assertTrue(text.endswith('Then run /goon test.'))
        self.assertEqual(goon.topic_text(), text)

    def test_topic_new_makes_another_and_says_to_drop_the_old_one(self):
        old = goon.topic_text()
        new = goon.topic_text(rotate=True)
        self.assertNotEqual(old, new)
        self.assertTrue(new.startswith('Made a new private topic, so drop the old one in the app.'))

    def test_a_topic_from_the_options_is_shown_as_such_and_not_changed(self):
        os.environ['CLAUDE_PLUGIN_OPTION_NTFY_TOPIC'] = 'mine'
        self.assertIn('Goon pushes to mine. The topic option is set', goon.topic_text(rotate=True))
        self.assertFalse((self.dir / 'topic').exists())

    def test_main_prints_the_topic_text(self):
        out = io.StringIO()
        with mock.patch.object(sys, 'stdout', out):
            goon.main(['topic'])
        self.assertIn('private ntfy topic', out.getvalue())

    def test_main_rotates_on_topic_new(self):
        with mock.patch.object(sys, 'stdout', io.StringIO()):
            goon.main(['topic'])
            old = (self.dir / 'topic').read_text()
            goon.main(['topic', 'new'])
        self.assertNotEqual((self.dir / 'topic').read_text(), old)


class SendTestTest(WithDirs):
    def setUp(self):
        super().setUp()
        self.server = FakeNtfy()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        patcher = mock.patch.object(goon, 'NTFY_URL', f'http://127.0.0.1:{self.server.server_port}/')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = {'ntfy_topic': 'secret-topic', 'notify_email': 'me@example.com', 'ntfy_token': ''}

    def test_one_push_goes_to_the_topic_without_the_email(self):
        text = goon.send_test(self.settings)
        self.assertEqual(text, 'Sent: it should show up in the ntfy app now.')
        [(path, headers, body)] = self.server.requests
        self.assertEqual(path, '/secret-topic')
        self.assertNotIn('Email', headers)
        self.assertEqual(headers['Title'], 'Claude Goon test')
        self.assertIn(b'the topic works', body)

    def test_a_failure_points_at_the_log(self):
        self.server.shutdown()
        self.server.server_close()
        with mock.patch('time.sleep'):
            text = goon.send_test(self.settings)
        self.assertTrue(text.startswith('Not sent;'))


class PowerTest(WithDirs):
    def test_goon_is_on_until_switched_off(self):
        self.assertTrue(goon.is_enabled())
        self.assertEqual(goon.power('status'), 'Goon is on.')

    def test_off_and_on_flip_the_switch_and_say_so(self):
        self.assertIn('Goon is off', goon.power('off'))
        self.assertFalse(goon.is_enabled())
        self.assertEqual(goon.power('on'), 'Goon is on.')
        self.assertTrue(goon.is_enabled())

    def test_the_command_exits_with_its_own_code_while_off_so_the_mod_can_tell_it_from_a_crash(self):
        for word, code in (('off', 3), ('status', 3), ('on', 0)):
            with mock.patch.object(sys, 'stdout', io.StringIO()):
                with self.assertRaises(SystemExit) as stopped:
                    goon.main(['power', word])
            self.assertEqual(stopped.exception.code, code, word)

    def test_a_session_is_not_resumed_while_off(self):
        goon.power('off')
        self.assertEqual(goon.decide(self.record(self.transcript(limit_row()))), 'off')

    def test_nothing_is_said_about_sessions_left_while_off(self):
        self.assertIsNone(goon.summary(Counter(off=2), '5-hour limit'))


class FakeNtfy(http.server.ThreadingHTTPServer):
    def __init__(self):
        super().__init__(('127.0.0.1', 0), FakeNtfyHandler)
        self.requests = []
        threading.Thread(target=self.serve_forever, daemon=True).start()


class FakeNtfyHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        self.server.requests.append((self.path, dict(self.headers), body))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, select, sys
record = {'cwd': os.getcwd(), 'argv': sys.argv[1:], 'message': json.loads(sys.stdin.readline())}
if os.environ.get('FAKE_MODE') == 'prompt':
    request = {'subtype': 'can_use_tool', 'tool_name': 'Write', 'input': {}}
    print(json.dumps({'type': 'control_request', 'request_id': 'r1', 'request': request}), flush=True)
    ready = select.select([sys.stdin], [], [], 0.5)[0]
    record['after'] = sys.stdin.readline() if ready else ''
else:
    print(json.dumps({'type': 'result', 'subtype': 'success', 'result': 'resumed ok'}), flush=True)
open(os.environ['FAKE_CALLS'], 'w').write(json.dumps(record))
sys.stdin.read()
"""


class WithFakeClaude(WithDirs):
    """A fake claude speaking stream-json, a fake ntfy, and the module's waits cut short."""

    def setUp(self):
        super().setUp()
        self.server = FakeNtfy()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.calls = self.root / 'claude-calls.json'
        fake = self.root / 'fake-claude'
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
        env = {
            'GOON_CLAUDE': str(fake),
            'FAKE_CALLS': str(self.calls),
            'CLAUDE_PLUGIN_OPTION_NTFY_TOPIC': 'secret-topic',
            'CLAUDE_PLUGIN_OPTION_RESUME_TEXT': 'carry on',
        }
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, value in (
            ('MARGIN', timedelta(0)),
            ('STAGGER', 0),
            ('LOOKUP_TRIES', 1),
            ('NTFY_URL', f'http://127.0.0.1:{self.server.server_port}/'),
        ):
            patch = mock.patch.object(goon, name, value)
            patch.start()
            self.addCleanup(patch.stop)
        # The supervisor is a detached process in real use; here it runs to its end inside the watcher.
        patch = mock.patch.object(goon, 'start_supervisor', lambda job: goon.supervise(job, goon.read_settings()))
        patch.start()
        self.addCleanup(patch.stop)

    def job(self):
        return {'binary': os.environ['GOON_CLAUDE'], 'session_id': SESSION, 'cwd': str(self.cwd), 'epoch': 1}


class SuperviseTest(WithFakeClaude):
    def test_the_text_goes_in_as_a_user_message_and_the_run_ends_on_its_result(self):
        with self.assertLogs('goon', 'INFO') as logged:
            code = goon.supervise(self.job(), goon.read_settings())
        record = json.loads(self.calls.read_text())
        self.assertEqual(code, 0)
        self.assertEqual(record['message'], {'type': 'user', 'message': {'role': 'user', 'content': 'carry on'}})
        self.assertEqual(record['cwd'], str(self.cwd.resolve()))
        self.assertIn('finished: resumed ok', '\n'.join(logged.output))

    def test_an_unset_text_falls_back_to_the_default(self):
        goon.supervise(self.job(), {**goon.read_settings(), 'resume_text': ''})
        self.assertEqual(json.loads(self.calls.read_text())['message']['message']['content'], choices.DEFAULT_MESSAGE)

    @mock.patch.dict(os.environ, {'FAKE_MODE': 'prompt'})
    @mock.patch.object(goon, 'PROMPT_WAIT', 1)
    def test_a_permission_prompt_is_held_and_reported_not_answered(self):
        with self.assertLogs('goon', 'INFO') as logged:
            code = goon.supervise(self.job(), goon.read_settings())
        self.assertEqual(json.loads(self.calls.read_text())['after'], '')
        self.assertNotEqual(code, 0)
        self.assertIn('waiting on a permission prompt for Write', '\n'.join(logged.output))
        [(_, _, body)] = self.server.requests
        self.assertEqual(body, b'A resumed session is waiting on a permission prompt for Write.')


class WatchTest(WithFakeClaude):
    """The whole path: a limit error in a transcript to a headless resume and a push."""

    def payload(self, session_id=SESSION):
        reset = int(datetime.now(timezone.utc).timestamp()) + 1
        path = self.root / f'{session_id}.jsonl'
        row = limit_row(at=datetime.now(timezone.utc), reset=reset)
        path.write_text(json.dumps(row) + '\n')
        return {'session_id': session_id, 'cwd': str(self.cwd), 'transcript_path': str(path), 'error': 'rate_limit'}

    def test_a_closed_session_is_resumed_in_its_directory_and_the_push_says_so(self):
        goon.watch(self.payload())
        record = json.loads(self.calls.read_text())
        self.assertEqual(record['cwd'], str(self.cwd.resolve()))
        self.assertEqual(record['argv'][:3], ['--resume', SESSION, '-p'])
        self.assertEqual(record['message']['message']['content'], 'carry on')
        [(path, headers, body)] = self.server.requests
        self.assertEqual(path, '/secret-topic')
        self.assertEqual(headers['Title'], 'Claude Goon')
        self.assertEqual(body, b'5-hour limit reset: resumed 1 closed session.')
        self.assertEqual([p.name for p in self.dir.glob('*.lock')], [])

    def test_one_watcher_resumes_every_session_registered_for_its_reset(self):
        first = self.payload()
        second = self.payload('99999999-2222-3333-4444-555555555555')
        goon.register(
            int(lw.find_limit(Path(second['transcript_path']), datetime.now(timezone.utc)).reset.timestamp()),
            second,
        )
        goon.watch(first)
        [(_, _, body)] = self.server.requests
        self.assertEqual(body, b'5-hour limit reset: resumed 2 closed sessions.')

    def test_launches_are_spaced_by_the_stagger(self):
        first = self.payload()
        second = self.payload('99999999-2222-3333-4444-555555555555')
        goon.register(
            int(lw.find_limit(Path(second['transcript_path']), datetime.now(timezone.utc)).reset.timestamp()),
            second,
        )
        real_sleep = goon.time.sleep
        with mock.patch.object(goon, 'STAGGER', 7):
            with mock.patch.object(goon.time, 'sleep', side_effect=lambda s: real_sleep(0.05)) as sleep:
                goon.watch(first)
        self.assertEqual(sleep.call_args_list.count(mock.call(7)), 1)

    def test_a_session_registered_after_the_last_look_is_still_resumed(self):
        late = self.payload('99999999-2222-3333-4444-555555555555')
        real, calls = goon.resume_all, []

        def resume_all(epoch):
            outcomes = real(epoch)
            if not calls:
                goon.register(epoch, late)
            calls.append(epoch)
            return outcomes

        with mock.patch.object(goon, 'resume_all', resume_all):
            goon.watch(self.payload())
        self.assertEqual(len(calls), 2)
        [(_, _, body)] = self.server.requests
        self.assertEqual(body, b'5-hour limit reset: resumed 2 closed sessions.')
        self.assertEqual([p.name for p in self.dir.glob('*.lock')], [])

    def test_a_spend_cap_reported_beside_the_window_does_not_hold_the_resume_back(self):
        payload = self.payload()
        row = json.loads(Path(payload['transcript_path']).read_text())
        row['apiErrorParams']['rate_limit_info'].update(
            overageDisabledReason='org_spend_cap_reached',
            overageResetsAt=row['apiErrorParams']['rate_limit_info']['resetsAt'] + 86400 * 20,
        )
        Path(payload['transcript_path']).write_text(json.dumps(row) + '\n')
        goon.watch(payload)
        self.assertTrue(self.calls.exists())
        [(_, _, body)] = self.server.requests
        self.assertEqual(body, b'5-hour limit reset: resumed 1 closed session.')

    def payload_resetting_in(self, days, **info):
        payload = self.payload()
        row = json.loads(Path(payload['transcript_path']).read_text())
        reset = int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())
        row['apiErrorParams']['rate_limit_info'].update(resetsAt=reset, overageResetsAt=reset, **info)
        Path(payload['transcript_path']).write_text(json.dumps(row) + '\n')
        return payload

    @mock.patch.object(goon, 'sleep_until')
    def test_a_window_reset_more_than_eight_days_off_is_logged_and_not_watched(self, sleep):
        with self.assertLogs('goon', 'INFO') as logged:
            goon.watch(self.payload_resetting_in(9))
        self.assertIn('unusable reset time', '\n'.join(logged.output))
        sleep.assert_not_called()
        self.assertFalse(self.calls.exists())

    @mock.patch.object(goon, 'sleep_until')
    def test_a_monthly_spend_cap_is_waited_for_beyond_eight_days(self, sleep):
        capped = {'rateLimitType': 'overage', 'overageDisabledReason': 'org_level_disabled_until'}
        goon.watch(self.payload_resetting_in(20, **capped))
        sleep.assert_called_once()
        self.assertTrue(self.calls.exists())

    def test_an_open_session_is_not_resumed_and_is_reported(self):
        self.open_session()
        goon.watch(self.payload())
        self.assertFalse(self.calls.exists())
        [(_, _, body)] = self.server.requests
        self.assertEqual(body, b'5-hour limit reset: 1 open session left to the Goon mod inside.')

    def test_the_email_and_token_travel_as_headers_when_set(self):
        env = {'CLAUDE_PLUGIN_OPTION_NOTIFY_EMAIL': 'me@example.com', 'CLAUDE_PLUGIN_OPTION_NTFY_TOKEN': 'tk_secret'}
        with mock.patch.dict(os.environ, env):
            goon.watch(self.payload())
        [(_, headers, body)] = self.server.requests
        self.assertEqual((headers['Email'], headers['Authorization']), ('me@example.com', 'Bearer tk_secret'))
        self.assertNotIn(b'me@example.com', body)

    def test_a_push_without_email_carries_neither_header(self):
        goon.watch(self.payload())
        [(_, headers, _)] = self.server.requests
        self.assertNotIn('Email', headers)
        self.assertNotIn('Authorization', headers)

    def test_an_empty_topic_option_pushes_to_the_private_topic(self):
        with mock.patch.dict(os.environ, {'CLAUDE_PLUGIN_OPTION_NTFY_TOPIC': ''}):
            goon.watch(self.payload())
        [(path, _, _)] = self.server.requests
        self.assertEqual(path, '/' + (self.dir / 'topic').read_text().strip())
        self.assertTrue(path.startswith('/claude-goon-'))

    @mock.patch.dict(os.environ, {'CLAUDE_PLUGIN_OPTION_QUIET_HOURS': '22:00-07:00'})
    @mock.patch.object(goon, 'sleep_until')
    @mock.patch.object(goon, 'after_quiet')
    def test_quiet_hours_push_the_resume_to_the_end_of_the_window(self, after, sleep):
        after.return_value = datetime.now(timezone.utc) + timedelta(hours=3)
        goon.watch(self.payload())
        self.assertEqual(after.call_args.args[1], '22:00-07:00')
        sleep.assert_called_once_with(after.return_value)

    @mock.patch.dict(os.environ, {'CLAUDE_PLUGIN_OPTION_QUIET_HOURS': 'night'})
    def test_an_invalid_quiet_hours_spec_is_logged_and_ignored(self):
        with self.assertLogs('goon', 'INFO') as logged:
            goon.watch(self.payload())
        self.assertIn("ignoring quiet hours 'night'", '\n'.join(logged.output))
        self.assertTrue(self.calls.exists())

    def test_while_off_nothing_is_resumed_or_pushed_and_the_pending_session_is_cleared(self):
        goon.power('off')
        goon.watch(self.payload())
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.server.requests, [])
        self.assertEqual(
            goon.take_pending(
                int(lw.find_limit(self.root / f'{SESSION}.jsonl', datetime.now(timezone.utc)).reset.timestamp())
            ),
            [],
        )

    def test_switching_on_before_the_reset_resumes_as_usual(self):
        goon.power('off')
        goon.power('on')
        goon.watch(self.payload())
        self.assertTrue(self.calls.exists())

    def test_a_transcript_with_no_limit_error_starts_nothing(self):
        path = self.transcript(chat_row())
        goon.watch({'session_id': SESSION, 'cwd': str(self.cwd), 'transcript_path': str(path)})
        self.assertFalse(self.calls.exists())


class HookEntryTest(WithDirs):
    def run_hook(self, payload):
        stdin = mock.Mock(buffer=io.BytesIO(json.dumps(payload).encode()))
        with mock.patch.object(sys, 'stdin', stdin), mock.patch.object(goon, 'start_watcher') as start:
            goon.main([])
        return start

    def test_a_limit_stop_starts_the_watcher(self):
        self.run_hook({'session_id': SESSION}).assert_called_once()

    def test_a_run_job_is_handed_to_the_supervisor(self):
        job = {'binary': '/bin/claude', 'session_id': SESSION, 'cwd': str(self.cwd), 'epoch': 1}
        stdin = mock.Mock(buffer=io.BytesIO())
        stdin.read = io.StringIO(json.dumps(job)).read
        with mock.patch.object(sys, 'stdin', stdin), mock.patch.object(goon, 'supervise', return_value=0) as supervise:
            goon.main(['run'])
        self.assertEqual(supervise.call_args.args[0], job)

    def test_a_subagent_stop_is_ignored(self):
        self.run_hook({'session_id': SESSION, 'agent_id': 'a1'}).assert_not_called()

    def test_the_log_is_readable_by_its_owner_only(self):
        self.run_hook({'session_id': SESSION})
        self.assertEqual((self.dir / 'log.txt').stat().st_mode & 0o777, 0o600)


class ResetCommandTest(WithDirs):
    def test_it_prints_the_reset_time_of_the_sessions_latest_limit(self):
        folder = self.claude / 'projects' / 'some-project'
        folder.mkdir(parents=True)
        (folder / f'{SESSION}.jsonl').write_text(
            json.dumps(limit_row(at=datetime.now(timezone.utc), reset=1793324400)) + '\n'
        )
        out = io.StringIO()
        with mock.patch.object(sys, 'stdout', out):
            goon.main(['reset', '--session', SESSION])
        self.assertEqual(
            json.loads(out.getvalue()), {'resetAt': 1793324400, 'resumeAt': 1793324460, 'window': 'five_hour'}
        )

    def test_it_prints_the_end_of_the_quiet_hours_as_the_resume_time(self):
        folder = self.claude / 'projects' / 'some-project'
        folder.mkdir(parents=True)
        (folder / f'{SESSION}.jsonl').write_text(
            json.dumps(limit_row(at=datetime.now(timezone.utc), reset=1793324400)) + '\n'
        )
        out = io.StringIO()
        with mock.patch.object(sys, 'stdout', out):
            with mock.patch.object(goon, 'after_quiet', return_value=utc(2026, 10, 30, 7, 0)):
                goon.main(['reset', '--session', SESSION, '--quiet', '22:00-07:00'])
        self.assertEqual(json.loads(out.getvalue())['resumeAt'], int(utc(2026, 10, 30, 7, 0).timestamp()))

    def test_it_prints_nothing_for_a_session_with_no_limit(self):
        out = io.StringIO()
        with mock.patch.object(sys, 'stdout', out), mock.patch.object(goon, 'LOOKUP_TRIES', 1):
            goon.main(['reset', '--session', SESSION])
        self.assertEqual(out.getvalue(), '')

    def test_it_prints_nothing_when_a_newer_error_that_is_no_limit_followed_the_limit(self):
        folder = self.claude / 'projects' / 'some-project'
        folder.mkdir(parents=True)
        now = datetime.now(timezone.utc)
        rows = [limit_row(at=now, reset=1793324400), limit_row(at=now, text='API Error: overloaded')]
        (folder / f'{SESSION}.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n')
        out = io.StringIO()
        with mock.patch.object(sys, 'stdout', out), mock.patch.object(goon, 'LOOKUP_TRIES', 1):
            goon.main(['reset', '--session', SESSION])
        self.assertEqual(out.getvalue(), '')


class ChoicesTest(WithDirs):
    def test_the_built_in_presets_include_the_ones_asked_for_and_added_ones_follow(self):
        choices.add_preset(self.dir, 'Go on and test')
        listed = choices.presets(self.dir)
        self.assertEqual(
            listed[:6],
            [
                'Go on',
                'Go on and review',
                'Go on and recap',
                'Go on and wrap up',
                'Go on, then review and recap',
                'Go on, then review and wrap up',
            ],
        )
        self.assertEqual(listed[6:], ['Go on and test'])

    def test_a_number_picks_a_preset_and_other_text_stands_for_itself(self):
        self.assertEqual(choices.resolve(self.dir, '3'), 'Go on and recap')
        self.assertEqual(choices.resolve(self.dir, '  fix   the tests  '), 'fix the tests')

    def test_a_number_without_a_preset_is_refused(self):
        for spec in ('0', '7'):
            with self.assertRaisesRegex(choices.ChoiceError, 'no preset'):
                choices.resolve(self.dir, spec)

    def test_an_empty_or_over_long_message_is_refused(self):
        with self.assertRaisesRegex(choices.ChoiceError, 'empty'):
            choices.resolve(self.dir, '   ')
        with self.assertRaisesRegex(choices.ChoiceError, 'limit is'):
            choices.resolve(self.dir, 'x' * (choices.MAX_MESSAGE + 1))

    def test_a_preset_is_added_once_and_removed_by_number_or_by_text(self):
        choices.add_preset(self.dir, 'one')
        choices.add_preset(self.dir, 'two')
        with self.assertRaisesRegex(choices.ChoiceError, 'already'):
            choices.add_preset(self.dir, 'one')
        self.assertEqual(choices.remove_preset(self.dir, '7'), 'one')
        self.assertEqual(choices.remove_preset(self.dir, 'two'), 'two')
        self.assertEqual(choices.presets(self.dir), list(choices.BUILT_IN_PRESETS))

    def test_a_built_in_or_unknown_preset_cannot_be_removed(self):
        with self.assertRaisesRegex(choices.ChoiceError, 'built in'):
            choices.remove_preset(self.dir, '1')
        with self.assertRaisesRegex(choices.ChoiceError, 'not a preset'):
            choices.remove_preset(self.dir, 'never added')

    def test_the_default_message_is_the_saved_one_then_the_option_then_the_built_in(self):
        self.assertEqual(choices.default_message(self.dir, ''), 'Go on')
        self.assertEqual(choices.default_message(self.dir, 'from option'), 'from option')
        choices.set_default_message(self.dir, 'saved')
        self.assertEqual(choices.default_message(self.dir, 'from option'), 'saved')
        choices.set_default_message(self.dir, None)
        self.assertEqual(choices.default_message(self.dir, 'from option'), 'from option')

    def test_a_session_message_beats_the_default_and_the_session_alone_is_affected(self):
        choices.set_default_message(self.dir, 'saved')
        choices.choose(self.dir, SESSION, message='mine')
        self.assertEqual(choices.message_for(self.dir, SESSION, ''), 'mine')
        self.assertEqual(choices.message_for(self.dir, OTHER, ''), 'saved')

    def test_choices_are_dropped_one_by_one_and_the_file_goes_with_the_last(self):
        choices.choose(self.dir, SESSION, enabled=False, message='mine')
        choices.choose(self.dir, SESSION, message=None)
        self.assertEqual(choices.session_choice(self.dir, SESSION), {'enabled': False})
        choices.choose(self.dir, SESSION, enabled=None)
        self.assertFalse(choices.session_file(self.dir, SESSION).exists())
        self.assertTrue(choices.is_enabled_for(self.dir, SESSION))

    def test_choices_are_kept_in_a_file_only_the_owner_can_read(self):
        choices.choose(self.dir, SESSION, message='mine')
        self.assertEqual(choices.session_file(self.dir, SESSION).stat().st_mode & 0o777, 0o600)

    def test_something_that_is_no_session_id_is_refused_and_reads_as_no_choice(self):
        with self.assertRaisesRegex(choices.ChoiceError, 'not a session id'):
            choices.choose(self.dir, '../escape', message='x')
        self.assertEqual(choices.session_choice(self.dir, '../escape'), {})
        self.assertEqual(choices.message_for(self.dir, '../escape', ''), 'Go on')


class SessionInfoTest(WithDirs):
    def info(self, *rows):
        return limits.session_info(self.transcript(*rows))

    def test_the_newest_of_each_fact_wins(self):
        rows = [
            {'type': 'ai-title', 'aiTitle': 'Old title'},
            {'type': 'ai-title', 'aiTitle': 'New   title'},
            {'type': 'last-prompt', 'lastPrompt': 'first'},
            {'type': 'last-prompt', 'lastPrompt': 'second'},
            {'type': 'cost-state', 'totalCostUSD': 0.5},
            {'type': 'cost-state', 'totalCostUSD': 1.5},
            {'type': 'assistant', 'gitBranch': 'old-branch', 'message': {'model': 'model-a'}},
            {'type': 'user', 'gitBranch': 'feature'},
            {'type': 'assistant', 'message': {'model': '<synthetic>'}},
        ]
        info = self.info(*rows)
        self.assertEqual(
            {k: v for k, v in info.items() if k != 'size'},
            {'title': 'New title', 'last_prompt': 'second', 'cost': 1.5, 'branch': 'feature', 'model': 'model-a'},
        )
        self.assertEqual(info['size'], (self.root / 'transcript.jsonl').stat().st_size)

    def test_the_newest_folder_is_the_sessions(self):
        rows = [
            {'type': 'user', 'cwd': '/first'},
            {'type': 'user', 'cwd': '/second'},
            {'type': 'user', 'isSidechain': True, 'cwd': '/agent'},
        ]
        self.assertEqual(self.info(*rows)['cwd'], '/second')

    def test_a_subagents_model_and_branch_are_not_the_sessions(self):
        row = {
            'type': 'assistant',
            'isSidechain': True,
            'gitBranch': 'agent-branch',
            'message': {'model': 'agent-model'},
        }
        self.assertNotIn('model', self.info(row))
        self.assertNotIn('branch', self.info(row))

    def test_values_of_the_wrong_type_are_left_out(self):
        rows = [{'type': 'ai-title', 'aiTitle': 5}, {'type': 'cost-state', 'totalCostUSD': 'a lot'}]
        self.assertEqual(set(self.info(*rows)), {'size'})

    def test_a_long_prompt_is_cut_on_one_line(self):
        prompt = limits.session_info(self.transcript({'type': 'last-prompt', 'lastPrompt': 'a\nb ' * 100}))[
            'last_prompt'
        ]
        self.assertEqual(len(prompt), 200)
        self.assertNotIn('\n', prompt)

    def test_an_unreadable_transcript_gives_nothing(self):
        self.assertEqual(limits.session_info(self.root / 'nope.jsonl'), {})


class WithPending(WithDirs):
    """Sessions registered for a reset, as the hook's watcher leaves them, with the plugin options unset."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('CLAUDE_PLUGIN_OPTION_RESUME_TEXT', None)

    def register(self, session_id=SESSION, epoch=1_800_000_000, **facts):
        goon.register(epoch, self.record(self.transcript(limit_row()), session_id), {**FACTS, **facts})


class ListTest(WithPending):
    def test_nothing_pending_says_so(self):
        self.assertEqual(goon.run_command('list'), 'No interrupted sessions are waiting.')

    def test_a_pending_session_is_listed_with_what_is_known_about_it(self):
        self.register()
        text = goon.run_command('list')
        for expected in (
            '1 interrupted session waiting. Goon is on; it sends "Go on" unless a session has its own message.',
            '11111111  Fix the importer',
            f'closed, {self.cwd} (main), 3.4 MB, $1.23 so far, model-a',
            'weekly limit, resets Jan 15, 08:00 UTC',
            'will send "Go on"',
            'last prompt: please fix the importer',
        ):
            self.assertIn(expected, text)

    def test_a_session_a_process_holds_is_listed_as_open(self):
        self.register()
        self.open_session()
        self.assertIn(f'open, {self.cwd}', goon.run_command('list'))

    def test_a_session_without_a_title_is_named_by_its_folder(self):
        self.register(title=None)
        self.assertIn(f'11111111  {self.cwd.name}', goon.run_command('list'))

    def test_the_message_a_session_will_get_and_whether_it_is_switched_off_show(self):
        self.register()
        goon.run_command('session 1111 message 3')
        self.assertIn('will send "Go on and recap" (chosen for this session)', goon.run_command('list'))
        goon.run_command('session 1111 off')
        self.assertIn('switched off for this session: nothing will be sent', goon.run_command('list'))

    def test_the_header_says_when_goon_is_off(self):
        self.register()
        goon.power('off')
        self.assertIn('Goon is off, so nothing is resumed', goon.run_command('list'))

    def test_sessions_come_in_the_order_of_their_resets(self):
        self.register(OTHER, epoch=1_800_000_000, title='Later')
        self.register(SESSION, epoch=1_700_000_000, title='Sooner')
        text = goon.run_command('list')
        self.assertLess(text.index('Sooner'), text.index('Later'))


class MessageCommandTest(WithPending):
    def test_presets_are_numbered_and_the_default_is_marked(self):
        text = goon.run_command('presets')
        self.assertIn('1. Go on  (default)', text)
        self.assertIn('5. Go on, then review and recap\n', text)
        goon.run_command('message 5')
        self.assertIn('5. Go on, then review and recap  (default)', goon.run_command('presets'))

    def test_a_number_or_text_sets_the_default_for_every_session_until_reset(self):
        self.assertIn('"Go on and recap"', goon.run_command('message 3'))
        self.assertEqual(choices.default_message(self.dir, ''), 'Go on and recap')
        self.assertIn('"fix the tests"', goon.run_command('message fix the tests'))
        self.assertIn('"Go on"', goon.run_command('message default'))
        self.assertEqual(choices.default_message(self.dir, ''), 'Go on')

    def test_message_alone_shows_it_without_changing_it(self):
        goon.run_command('message 3')
        self.assertIn('"Go on and recap"', goon.run_command('message'))

    def test_a_bad_number_is_explained_and_changes_nothing(self):
        self.assertIn('There is no preset 9', goon.run_command('message 9'))
        self.assertEqual(choices.default_message(self.dir, ''), 'Go on')

    def test_presets_are_added_and_removed(self):
        self.assertEqual(goon.run_command('preset add Go on and test'), 'Added "Go on and test".')
        self.assertIn('7. Go on and test', goon.run_command('presets'))
        self.assertEqual(goon.run_command('preset remove 7'), 'Removed "Go on and test".')
        self.assertNotIn('Go on and test', goon.run_command('presets'))
        self.assertIn('Use /goon preset add', goon.run_command('preset'))

    def test_the_default_option_is_the_message_while_nothing_was_set(self):
        os.environ['CLAUDE_PLUGIN_OPTION_RESUME_TEXT'] = 'from option'
        self.assertIn('"from option"', goon.run_command('message'))

    def test_free_text_on_the_command_line_is_taken_as_words(self):
        with mock.patch.object(sys, 'stdout', io.StringIO()) as out:
            goon.main(['command', 'message', 'Go', 'on', 'and', 'recap'])
        self.assertIn('"Go on and recap"', out.getvalue())
        self.assertEqual(choices.default_message(self.dir, ''), 'Go on and recap')

    def test_an_unknown_command_gets_the_usage(self):
        self.assertEqual(goon.run_command('dance'), goon.USAGE)


class SessionCommandTest(WithPending):
    def test_a_session_is_switched_off_and_on_by_the_start_of_its_id(self):
        self.register()
        self.assertEqual(goon.run_command('session 1111 off'), '11111111: switched off.')
        self.assertFalse(choices.is_enabled_for(self.dir, SESSION))
        self.assertEqual(goon.run_command('session 11111111 on'), '11111111: switched on.')
        self.assertTrue(choices.is_enabled_for(self.dir, SESSION))

    def test_a_session_gets_its_own_message_by_number_or_text_and_gives_it_up(self):
        self.register()
        self.assertEqual(goon.run_command('session 1111 message 4'), '11111111: will send "Go on and wrap up".')
        self.assertEqual(choices.message_for(self.dir, SESSION, ''), 'Go on and wrap up')
        goon.run_command('session 1111 message check the logs')
        self.assertEqual(choices.message_for(self.dir, SESSION, ''), 'check the logs')
        goon.run_command('session 1111 message default')
        self.assertEqual(choices.message_for(self.dir, SESSION, ''), 'Go on')

    def test_reset_drops_every_choice_for_the_session(self):
        self.register()
        goon.run_command('session 1111 off')
        goon.run_command('session 1111 message 3')
        goon.run_command('session 1111 reset')
        self.assertEqual(choices.session_choice(self.dir, SESSION), {})

    def test_a_prefix_that_fits_two_sessions_or_none_is_refused(self):
        self.register()
        self.register(TWIN)
        self.assertIn('2 pending sessions start with 11111111', goon.run_command('session 11111111 off'))
        self.assertIn('No pending session starts with 7777', goon.run_command('session 7777 off'))
        self.assertEqual(choices.session_choice(self.dir, SESSION), {})

    def test_a_short_prefix_is_refused(self):
        self.register()
        self.assertIn('first four or more characters', goon.run_command('session 11 off'))

    def test_a_whole_id_is_accepted_before_the_session_is_registered(self):
        self.assertEqual(goon.run_command(f'session {OTHER} message 4'), '99999999: will send "Go on and wrap up".')
        self.assertEqual(choices.message_for(self.dir, OTHER, ''), 'Go on and wrap up')

    def test_a_session_command_without_an_action_gets_the_usage(self):
        self.register()
        for words in ('session', 'session 1111', 'session 1111 sing'):
            self.assertEqual(goon.run_command(words), goon.SESSION_USAGE, words)


class PlanTest(WithPending):
    def plan(self, session_id=SESSION):
        with mock.patch.object(sys, 'stdout', io.StringIO()) as out:
            goon.main(['plan', '--session', session_id])
        return json.loads(out.getvalue())

    def exit_code(self, session_id=SESSION):
        with mock.patch.object(sys, 'stdout', io.StringIO()), self.assertRaises(SystemExit) as stopped:
            goon.main(['plan', '--session', session_id])
        return stopped.exception.code

    def test_the_plan_is_the_message_and_the_presets_to_pick_from(self):
        self.assertEqual(self.plan(), {'message': 'Go on', 'presets': list(choices.BUILT_IN_PRESETS)})

    def test_the_session_message_and_a_changed_default_show_at_once(self):
        choices.set_default_message(self.dir, 'saved')
        self.assertEqual(self.plan()['message'], 'saved')
        choices.choose(self.dir, SESSION, message='mine')
        self.assertEqual(self.plan()['message'], 'mine')
        self.assertEqual(self.plan(OTHER)['message'], 'saved')

    def test_the_command_exits_with_the_off_code_while_goon_or_the_session_is_off(self):
        choices.choose(self.dir, SESSION, enabled=False)
        self.assertEqual(self.exit_code(), goon.OFF_EXIT)
        self.assertEqual(self.plan(OTHER)['message'], 'Go on')
        goon.power('off')
        self.assertEqual(self.exit_code(OTHER), goon.OFF_EXIT)

    def test_the_session_is_required(self):
        with mock.patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit) as stopped:
            goon.main(['plan'])
        self.assertEqual(stopped.exception.code, 2)


class SessionChoicesInTheWatcherTest(WithFakeClaude, WithPending):
    def payload(self, session_id=SESSION, title=None):
        reset = int(datetime.now(timezone.utc).timestamp()) + 1
        rows = [limit_row(at=datetime.now(timezone.utc), reset=reset)]
        if title:
            rows.insert(0, {'type': 'ai-title', 'aiTitle': title})
        path = self.root / f'{session_id}.jsonl'
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return {'session_id': session_id, 'cwd': str(self.cwd), 'transcript_path': str(path), 'error': 'rate_limit'}

    def blocked(self, session_id):
        return self.record(self.transcript(limit_row()), session_id)

    def test_a_closed_session_switched_off_is_left_alone_and_an_open_one_is_left_to_its_mod(self):
        choices.choose(self.dir, SESSION, enabled=False)
        self.assertEqual(goon.decide(self.blocked(SESSION)), 'session-off')
        self.open_session()
        self.assertEqual(goon.decide(self.blocked(SESSION)), 'open')

    def test_a_session_switched_off_is_not_resumed_and_not_counted_as_skipped(self):
        choices.choose(self.dir, SESSION, enabled=False)
        goon.watch(self.payload())
        self.assertFalse(self.calls.exists())
        self.assertEqual(self.server.requests, [])
        self.assertIsNone(goon.summary(Counter({'session-off': 2}), '5-hour limit'))

    def test_the_sessions_own_message_is_what_the_resumed_run_is_sent(self):
        choices.choose(self.dir, SESSION, message='Go on and recap')
        goon.watch(self.payload())
        self.assertEqual(json.loads(self.calls.read_text())['message']['message']['content'], 'Go on and recap')

    def test_a_message_changed_while_the_watcher_waits_is_the_one_sent(self):
        goon.register(100, self.record(Path('t.jsonl'), SESSION))
        goon.run_command('message 5')
        with mock.patch.object(goon, 'decide', return_value='resume'):
            with mock.patch.object(goon, 'start_supervisor') as start:
                goon.resume_all(100)
        self.assertEqual(start.call_args.args[0]['message'], 'Go on, then review and recap')

    def test_choices_for_a_handled_session_are_forgotten_but_an_open_ones_are_kept_for_its_mod(self):
        for session_id in (SESSION, OTHER):
            goon.register(100, self.record(Path('t.jsonl'), session_id))
            choices.choose(self.dir, session_id, message='mine')
        with mock.patch.object(
            goon, 'decide', side_effect=lambda record: 'open' if record['session_id'] == OTHER else 'continued'
        ):
            goon.resume_all(100)
        self.assertEqual(choices.session_choice(self.dir, SESSION), {})
        self.assertEqual(choices.session_choice(self.dir, OTHER), {'message': 'mine'})

    def test_what_is_known_about_the_session_is_kept_with_its_registration(self):
        seen = []

        def look(epoch):
            seen.extend(goon.pending_records())
            goon.take_pending(epoch)
            return []

        with mock.patch.object(goon, 'resume_all', look):
            goon.watch(self.payload(title='Fix the importer'))
        [(epoch, record)] = seen
        self.assertEqual((record['title'], record['limit']), ('Fix the importer', '5-hour limit'))
        self.assertEqual(record['session_id'], SESSION)
        self.assertIn('interrupted', record)
        self.assertGreater(record['size'], 0)

    def test_the_push_names_the_sessions_only_when_the_option_is_on(self):
        goon.watch(self.payload(title='Fix the importer'))
        [(_, _, body)] = self.server.requests
        self.assertEqual(body, b'5-hour limit reset: resumed 1 closed session.')

    @mock.patch.dict(os.environ, {'CLAUDE_PLUGIN_OPTION_PUSH_DETAILS': 'true'})
    def test_with_the_option_on_the_push_names_each_session_and_what_became_of_it(self):
        goon.watch(self.payload(title='Fix the importer'))
        [(_, _, body)] = self.server.requests
        self.assertEqual(body, b'5-hour limit reset: resumed 1 closed session.\n- Fix the importer: resumed')

    @mock.patch.dict(os.environ, {'CLAUDE_PLUGIN_OPTION_PUSH_DETAILS': 'true', 'FAKE_MODE': 'prompt'})
    @mock.patch.object(goon, 'PROMPT_WAIT', 1)
    def test_a_held_prompt_names_its_session_when_the_option_is_on(self):
        goon.supervise({**self.job(), 'label': 'Fix the importer'}, goon.read_settings())
        [(_, _, body)] = self.server.requests
        self.assertEqual(body, b'"Fix the importer" is waiting on a permission prompt for Write.')


class ModOnlyTest(WithPending):
    """Sessions only the mod knows about: it armed them, the StopFailure hook never registered them."""

    def stopped_session(self, session_id=SESSION, *more):
        now = datetime.now(timezone.utc)
        rows = [
            {'type': 'ai-title', 'aiTitle': 'Fix the importer'},
            {'type': 'user', 'cwd': str(self.cwd), 'gitBranch': 'main', 'timestamp': stamp(now)},
            limit_row(at=now, reset=int(now.timestamp()) + 3600),
            *more,
        ]
        path = self.claude / 'projects' / 'proj' / f'{session_id}.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return path

    def resume_at(self):
        return int(datetime.now(timezone.utc).timestamp()) + 3660

    def test_what_the_transcript_says_is_kept_with_the_session(self):
        path = self.stopped_session()
        goon.track(SESSION, self.resume_at())
        [(_, note)] = goon.pending_records()
        self.assertEqual(
            (note['title'], note['cwd'], note['branch'], note['limit'], note['source']),
            ('Fix the importer', str(self.cwd), 'main', '5-hour limit', 'mod'),
        )
        self.assertEqual(note['transcript_path'], str(path))
        self.assertEqual(note['resume_at'], self.resume_at())

    def test_a_session_with_no_transcript_or_no_id_is_ignored(self):
        goon.track(OTHER, self.resume_at())
        goon.track('../escape', self.resume_at())
        self.assertEqual(goon.pending_records(), [])

    def test_it_is_listed_as_continued_by_the_mod_inside_while_the_session_is_open(self):
        self.stopped_session()
        goon.track(SESSION, self.resume_at())
        self.open_session()
        text = goon.run_command('list')
        self.assertIn('11111111  Fix the importer', text)
        self.assertIn('open,', text)
        self.assertIn('only the Goon mod inside the session continues it, at', text)

    def test_a_closed_one_is_listed_as_continued_by_nobody_unless_reopened(self):
        self.stopped_session()
        goon.track(SESSION, self.resume_at())
        text = goon.run_command('list')
        self.assertIn('closed,', text)
        self.assertIn('nothing continues it unless you reopen it before', text)

    def test_a_session_the_hook_registered_too_is_listed_once_as_the_hooks(self):
        self.stopped_session()
        self.register()
        goon.track(SESSION, self.resume_at())
        text = goon.run_command('list')
        self.assertEqual(text.count('11111111  '), 1)
        self.assertNotIn('only the Goon mod', text)

    def test_its_message_and_switch_can_be_chosen_like_any_other(self):
        self.stopped_session()
        goon.track(SESSION, self.resume_at())
        goon.run_command('session 1111 message 4')
        self.assertIn('will send "Go on and wrap up" (chosen for this session)', goon.run_command('list'))
        goon.run_command('session 1111 off')
        self.assertIn('switched off for this session', goon.run_command('list'))

    def test_a_note_long_after_its_resume_time_is_not_listed_and_the_next_track_drops_it(self):
        self.stopped_session()
        goon.track(SESSION, int(datetime.now(timezone.utc).timestamp()) - 2 * 86400)
        self.assertEqual(goon.pending_records(), [])
        self.stopped_session(OTHER)
        goon.track(OTHER, self.resume_at())
        self.assertEqual([p.stem for p in (self.dir / 'armed').glob('*.json')], [OTHER])

    def test_a_session_that_has_moved_on_is_marked_as_continued(self):
        self.stopped_session(SESSION, chat_row('user'), chat_row('assistant'))
        goon.track(SESSION, self.resume_at())
        self.assertIn('already continued: Goon will leave it alone', goon.run_command('list'))

    def test_a_session_the_hook_registered_and_that_moved_on_is_marked_too(self):
        goon.register(
            1_800_000_000, self.record(self.transcript(limit_row(), chat_row('user'), chat_row('assistant'))), FACTS
        )
        self.assertIn('already continued: Goon will leave it alone', goon.run_command('list'))

    def test_the_command_takes_the_session_and_the_time_it_is_armed_for(self):
        with mock.patch.object(goon, 'track') as track:
            goon.main(['track', '--session', SESSION, '--resume-at', '123'])
        track.assert_called_once_with(SESSION, 123)
        with mock.patch.object(sys, 'stderr', io.StringIO()), self.assertRaises(SystemExit) as stopped:
            goon.main(['track', '--session', SESSION])
        self.assertEqual(stopped.exception.code, 2)

    def test_the_cwd_the_hook_gives_is_not_replaced_by_the_one_in_the_transcript(self):
        goon.register(100, self.record(Path('t.jsonl')), {'cwd': '/elsewhere'})
        [(_, record)] = goon.pending_records()
        self.assertEqual(record['cwd'], str(self.cwd))


class DetailsTest(unittest.TestCase):
    def test_only_the_listed_words_turn_the_option_on(self):
        for value, expected in (
            ('true', True),
            ('TRUE', True),
            ('1', True),
            ('yes', True),
            ('false', False),
            ('', False),
        ):
            self.assertEqual(goon.wants_details({'push_details': value}), expected, value)

    def test_a_session_is_named_by_its_title_else_its_folder_and_sessions_nobody_acted_on_are_left_out(self):
        results = [
            ({'session_id': 'a', 'cwd': '/w/app', 'title': 'Fix it'}, 'resumed'),
            ({'session_id': 'b', 'cwd': '/w/api'}, 'open'),
            ({'session_id': 'c', 'cwd': '/w/x'}, 'continued'),
            ({'session_id': 'd', 'cwd': '/w/y'}, 'off'),
        ]
        self.assertEqual(
            goon.session_lines(results),
            '- Fix it: resumed\n- api: left to the Goon mod inside',
        )

    def test_a_long_list_is_cut(self):
        results = [({'session_id': str(n), 'cwd': f'/w/{n}'}, 'resumed') for n in range(goon.MAX_DETAIL_LINES + 3)]
        lines = goon.session_lines(results).splitlines()
        self.assertEqual(len(lines), goon.MAX_DETAIL_LINES + 1)
        self.assertEqual(lines[-1], 'and 3 more')


class VendoredTest(unittest.TestCase):
    def test_the_copy_of_limitwatch_matches_the_hash_in_its_header(self):
        _, header, body = (HOOKS / 'limitwatch.py').read_text().split('\n', 2)
        stated = re.search(r'sha256: ([0-9a-f]{64})', header).group(1)
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), stated)


if __name__ == '__main__':
    unittest.main()
