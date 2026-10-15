"""Exercise reply lifetimes through the bridge CLI and real local sockets."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

SCRIPT = Path(__file__).with_name('claude_peer.py')


class ReplyLifetimeTests(unittest.TestCase):
    def setUp(self):
        scratch = Path.home() / '.claude-scratchpad'
        scratch.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='cc-test-', dir=scratch)
        self.base = Path(self.temp.name)
        self.config = self.base / 'config'
        registry = self.config / 'sessions'
        registry.mkdir(parents=True)
        self.socket_path = self.base / f'{os.getpid()}.sock'
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.socket_path))
        self.socket_path.chmod(0o600)
        self.server.listen(4)
        self.server.settimeout(5)
        self.session_id = str(uuid.uuid4())
        (registry / f'{os.getpid()}.json').write_text(json.dumps({
            'pid': os.getpid(), 'sessionId': self.session_id,
            'messagingSocketPath': str(self.socket_path), 'name': 'late-reply-fixture',
            'cwd': str(self.base), 'status': 'idle',
        }))
        self.env = {
            **os.environ, 'CLAUDE_CONFIG_DIR': str(self.config),
            'CLAUDE_PEER_DATA_DIR': str(self.base / 'data'),
        }
        self.env.pop('CODEX_THREAD_ID', None)
        self.inboxes = set()
        self.processes = []

    def tearDown(self):
        self.inboxes.update(path.parent.name for path in
                            (self.base / 'data' / 'inboxes').glob('*/state.json'))
        for inbox in self.inboxes:
            self.cli('close', '--inbox', inbox)
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.server.close()
        self.temp.cleanup()

    def cli(self, *args, input=None):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], env=self.env,
            capture_output=True, text=True, timeout=10, input=input,
        )

    def start_send(self, wait='0.05', inbox=None, no_dispatch=False):
        args = [
            sys.executable, '-u', str(SCRIPT), 'send', '--pid', str(os.getpid()),
            '--wait', wait, '--message', 'Please reply when the build finishes.',
        ]
        if inbox:
            args += ['--inbox', inbox]
        if no_dispatch:
            args += ['--no-dispatch']
        process = subprocess.Popen(args, env=self.env, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        self.processes.append(process)
        conn, _ = self.server.accept()
        with conn:
            conn.settimeout(5)
            data = b''
            while chunk := conn.recv(65536):
                data += chunk
        frame = json.loads(data)
        self.assertEqual(frame['session_id'], self.session_id)
        sent = json.loads(process.stdout.readline())
        self.assertEqual(sent['event'], 'sent', sent)
        if sent.get('inbox'):
            self.inboxes.add(sent['inbox'])
        return process, frame, sent

    def reply(self, address, text, message_id=None, frame_type='user'):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(address.removeprefix('uds:'))
            client.sendall((json.dumps({
                'type': frame_type, 'msgV': 1, 'msg_id': message_id or str(uuid.uuid4()),
                'from': 'uds:' + str(self.socket_path),
                'message': {'role': 'user', 'content': text},
            }) + '\n').encode())

    def read_messages(self, inbox, after=0):
        result = self.cli('read', '--inbox', inbox, '--after', str(after), '--wait', '1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()]

    def enable_queue_fixture(self):
        self.queue_log = self.base / 'queued.jsonl'
        self.owner_thread = str(uuid.uuid4())
        executable = self.base / 'codex'
        executable.write_text(
            f'#!{sys.executable}\n'
            'import json, os, sys, time\n'
            'from pathlib import Path\n'
            'if sys.argv[1:] == ["queue", "--help"]:\n'
            '    sys.exit(0)\n'
            'with Path(os.environ["QUEUE_LOG"]).open("a") as handle:\n'
            '    handle.write(json.dumps(sys.argv[1:]) + "\\n")\n'
            'crash = Path(os.environ["QUEUE_LOG"]).with_suffix(".crash")\n'
            'if crash.exists():\n'
            '    crash.unlink()\n'
            '    os.kill(os.getppid(), 9)\n'
            'while Path(os.environ["QUEUE_LOG"]).with_suffix(".block").exists():\n'
            '    time.sleep(0.05)\n'
            'if Path(os.environ["QUEUE_LOG"]).with_suffix(".fail").exists():\n'
            '    sys.exit("Queue is unavailable")\n'
            'print("Queued message fixture for thread " + sys.argv[3])\n'
        )
        executable.chmod(0o700)
        self.env.update(CODEX_THREAD_ID=self.owner_thread, QUEUE_LOG=str(self.queue_log),
                        PATH=str(self.base) + os.pathsep + self.env.get('PATH', ''))

    def queued(self, count=1):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.queue_log.exists():
                rows = [json.loads(line) for line in self.queue_log.read_text().splitlines()]
                if len(rows) >= count:
                    return rows
            time.sleep(0.02)
        self.fail('The persistent receiver did not dispatch the saved reply')

    def wait_dispatch_status(self, status):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = json.loads(self.cli('inboxes').stdout)
            if state['dispatch_status'] == status:
                return state
            time.sleep(0.02)
        self.fail(f'Dispatch did not reach {status}: {state}')

    def test_register_reuses_chat_address_and_exposes_read_only_discovery(self):
        self.enable_queue_fixture()
        result = self.cli('register', '--name', 'Powder day review', '--cwd', str(self.base))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first = json.loads(result.stdout)
        self.inboxes.add(first['inbox'])
        result = self.cli('register', '--name', 'Powder day review')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        second = json.loads(result.stdout)
        self.assertEqual(second['inbox'], first['inbox'])
        self.assertEqual(second['reply_address'], first['reply_address'])
        discovered = self.cli('chats')
        rows = [json.loads(line) for line in discovered.stdout.splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['name'], 'Powder day review')
        self.assertEqual(rows[0]['cwd'], str(self.base))
        self.assertEqual(rows[0]['reply_address'], first['reply_address'])
        self.assertTrue(rows[0]['open'])
        self.assertTrue(rows[0]['receiver_wakes_agent'])
        self.assertNotIn('message', rows[0])

    def test_registration_adopts_existing_exchange_and_default_sends_reuse_it(self):
        self.enable_queue_fixture()
        process, frame, first = self.start_send(wait='0')
        process.communicate(timeout=5)
        result = self.cli('register', '--name', 'Existing chat')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        registered = json.loads(result.stdout)
        self.assertEqual(registered['reply_address'], first['reply_address'])
        process, _, second = self.start_send(wait='0')
        process.communicate(timeout=5)
        self.assertEqual(second['inbox'], first['inbox'])

    def test_reconnect_preserves_address_unread_messages_and_sequence(self):
        self.enable_queue_fixture()
        result = self.cli('register', '--name', 'Persistent chat')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        first = json.loads(result.stdout)
        self.inboxes.add(first['inbox'])
        self.reply(first['reply_address'], 'Before restart.', message_id='repeat-me')
        self.read_messages(first['inbox'])
        self.cli('close', '--inbox', first['inbox'])
        result = self.cli('register')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        second = json.loads(result.stdout)
        self.assertEqual(second['reply_address'], first['reply_address'])
        self.reply(second['reply_address'], 'Before restart.', message_id='repeat-me')
        self.reply(second['reply_address'], 'After restart.')
        rows = self.read_messages(first['inbox'], after=1)
        self.assertEqual(rows[0]['seq'], 2)
        self.assertEqual(rows[0]['frame']['message']['content'], 'After restart.')
        self.assertEqual(json.loads(self.cli('inboxes').stdout)['unread_count'], 2)

    def test_outgoing_mode_is_explicit_and_unknown_is_not_asserted(self):
        self.enable_queue_fixture()
        result = self.cli('register', '--sender-mode', 'bypass')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.inboxes.add(json.loads(result.stdout)['inbox'])
        process, frame, _ = self.start_send(wait='0')
        process.communicate(timeout=5)
        self.assertEqual(frame['from_mode'], 'bypass')
        self.assertIn('from-mode="bypass"', frame['message']['content'])
        self.cli('register', '--sender-mode', 'unknown')
        process, frame, _ = self.start_send(wait='0')
        process.communicate(timeout=5)
        self.assertNotIn('from_mode', frame)
        self.assertNotIn('from-mode=', frame['message']['content'])

    def test_dispatch_batches_saved_burst_without_consuming_replies(self):
        self.enable_queue_fixture()
        process, frame, sent = self.start_send(wait='0', no_dispatch=True)
        process.communicate(timeout=5)
        for text in ('First finding.', 'Second finding.', 'Final finding.'):
            self.reply(frame['from'], text)
        self.read_messages(sent['inbox'], after=2)
        result = self.cli('dispatch', '--inbox', sent['inbox'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.queued()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = json.loads(self.cli('inboxes').stdout)
            if state['dispatch_cursor'] == 3:
                break
            time.sleep(0.02)
        self.assertEqual(state['dispatch_cursor'], 3)
        self.assertEqual(len(self.queued()), 1)
        self.assertIn(f"{sent['inbox']}:3", self.queued()[0][4])
        self.assertEqual(state['unread_count'], 3)
        self.assertEqual(state['acknowledged_cursor'], 0)

    def test_idle_subscription_waits_for_completion_and_sends_one_correlated_notice(self):
        self.enable_queue_fixture()
        result = self.cli('register', '--sender-mode', 'prompting')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        registered = json.loads(result.stdout)
        self.inboxes.add(registered['inbox'])
        subscription = {'type': 'control', 'action': 'notify_when_idle', 'msgV': 1,
                        'msg_id': str(uuid.uuid4()), 'from': 'uds:' + str(self.socket_path)}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(registered['reply_address'].removeprefix('uds:'))
            client.sendall((json.dumps(subscription) + '\n').encode())
        self.read_messages(registered['inbox'])
        self.assertFalse(self.queue_log.exists())
        payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': self.owner_thread,
                              'turn-id': 'powder-turn', 'last-assistant-message': 'Review finished.'})
        result = self.cli('notify', payload)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        conn, _ = self.server.accept()
        with conn:
            notice = json.loads(conn.recv(65536))
        self.assertEqual(notice['action'], 'peer_idle_notice')
        self.assertEqual(notice['orig_msg_id'], subscription['msg_id'])
        self.assertEqual(notice['state'], 'idle')
        self.assertIsInstance(notice['finished_at'], int)
        self.assertEqual(notice['from'], registered['reply_address'])
        self.assertEqual(notice['from_mode'], 'prompting')
        self.assertEqual(self.cli('notify', payload).returncode, 0)
        self.server.settimeout(0.2)
        with self.assertRaises(socket.timeout):
            self.server.accept()

    def test_idle_does_not_answer_a_new_subscription_from_an_untracked_old_completion(self):
        self.enable_queue_fixture()
        registered = json.loads(self.cli('register').stdout)
        self.inboxes.add(registered['inbox'])
        complete = {'type': 'agent-turn-complete', 'thread-id': self.owner_thread,
                    'turn-id': 'old-turn'}
        self.cli('notify', json.dumps(complete))
        time.sleep(0.02)
        subscription = {'type': 'control', 'action': 'notify_when_idle', 'msgV': 1,
                        'msg_id': str(uuid.uuid4()), 'from': 'uds:' + str(self.socket_path)}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(registered['reply_address'].removeprefix('uds:'))
            client.sendall((json.dumps(subscription) + '\n').encode())
        self.read_messages(registered['inbox'])
        self.server.settimeout(0.2)
        with self.assertRaises(socket.timeout):
            self.server.accept()
        self.cli('notify', json.dumps(complete))
        with self.assertRaises(socket.timeout):
            self.server.accept()
        busy = {'hook_event_name': 'UserPromptSubmit', 'session_id': self.owner_thread,
                'turn_id': 'new-turn', 'permission_mode': 'default'}
        self.cli('hook', input=json.dumps(busy))
        self.cli('notify', json.dumps(complete))
        with self.assertRaises(socket.timeout):
            self.server.accept()
        complete['turn-id'] = 'new-turn'
        self.cli('notify', json.dumps(complete))
        conn, _ = self.server.accept()
        with conn:
            self.assertEqual(json.loads(conn.recv(65536))['orig_msg_id'], subscription['msg_id'])

    def test_register_recovers_a_killed_receiver_at_the_same_address(self):
        self.enable_queue_fixture()
        registered = json.loads(self.cli('register').stdout)
        self.inboxes.add(registered['inbox'])
        before = json.loads(self.cli('inboxes').stdout)
        os.kill(before['pid'], signal.SIGKILL)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if not json.loads(self.cli('inboxes').stdout)['open']:
                break
            time.sleep(0.02)
        result = self.cli('register')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['reply_address'], registered['reply_address'])
        self.reply(registered['reply_address'], 'Recovery succeeded.')
        self.assertEqual(self.read_messages(registered['inbox'])[0]['seq'], 1)

    def test_registration_and_ack_keep_the_active_turn_completion_guard(self):
        self.enable_queue_fixture()
        registered = json.loads(self.cli('register').stdout)
        self.inboxes.add(registered['inbox'])
        for operation in ('register', 'ack'):
            with self.subTest(operation=operation):
                turn = 'powder-' + operation
                event = {'hook_event_name': 'UserPromptSubmit', 'session_id': self.owner_thread,
                         'turn_id': turn}
                self.assertEqual(self.cli('hook', input=json.dumps(event)).returncode, 0)
                args = (['register'] if operation == 'register' else
                        ['ack', '--inbox', registered['inbox'], '--through', '0'])
                self.assertEqual(self.cli(*args).returncode, 0)
                complete = {'type': 'agent-turn-complete', 'thread-id': self.owner_thread,
                            'turn-id': 'late-earlier-' + operation}
                self.assertEqual(self.cli('notify', json.dumps(complete)).returncode, 0)
                self.assertEqual(json.loads(self.cli('chats').stdout)['activity'], 'busy')
                complete['turn-id'] = turn
                self.assertEqual(self.cli('notify', json.dumps(complete)).returncode, 0)
                self.assertEqual(json.loads(self.cli('chats').stdout)['activity'], 'idle')

    def test_listing_stops_quietly_when_a_pipeline_reader_closes(self):
        registry = self.config / 'sessions'
        sample = (registry / f'{os.getpid()}.json').read_text()
        for index in range(200):
            (registry / f'listing-{index}.json').write_text(sample)
        with subprocess.Popen([sys.executable, str(SCRIPT), 'list'], env=self.env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
            self.assertTrue(process.stdout.readline())
            process.stdout.close()
            result = process.wait(timeout=5)
            self.assertIn(result, (0, -signal.SIGPIPE))
            self.assertEqual(process.stderr.read(), '')

    def test_hook_modes_follow_native_messaging_permission_classes(self):
        self.enable_queue_fixture()
        registered = json.loads(self.cli('register', '--sender-mode', 'prompting').stdout)
        self.inboxes.add(registered['inbox'])
        for permission, expected in [('bypassPermissions', 'bypass'), ('default', 'prompting'),
                                     ('dontAsk', 'prompting'), ('auto', 'prompting')]:
            with self.subTest(permission=permission):
                event = {'hook_event_name': 'UserPromptSubmit', 'session_id': self.owner_thread,
                         'turn_id': 'powder-turn', 'permission_mode': permission}
                self.assertEqual(self.cli('hook', input=json.dumps(event)).returncode, 0)
                process, frame, _ = self.start_send(wait='0')
                process.communicate(timeout=5)
                self.assertEqual(frame.get('from_mode'), expected)

    def test_unrecognised_hook_mode_clears_a_previously_declared_mode(self):
        self.enable_queue_fixture()
        registered = json.loads(self.cli('register', '--sender-mode', 'prompting').stdout)
        self.inboxes.add(registered['inbox'])
        event = {'hook_event_name': 'UserPromptSubmit', 'session_id': self.owner_thread,
                 'turn_id': 'powder-turn', 'permission_mode': 'future-permission-policy'}
        self.assertEqual(self.cli('hook', input=json.dumps(event)).returncode, 0)
        process, frame, _ = self.start_send(wait='0')
        process.communicate(timeout=5)
        self.assertNotIn('from_mode', frame)
        self.assertNotIn('from-mode=', frame['message']['content'])

    def test_notify_also_runs_the_previously_configured_notifier(self):
        output = self.base / 'forwarded.json'
        executable = self.base / 'previous-notifier'
        executable.write_text(f'#!{sys.executable}\nimport sys\nfrom pathlib import Path\n'
                              f'Path({str(output)!r}).write_text(sys.argv[1])\n')
        executable.chmod(0o700)
        data = self.base / 'data'
        data.mkdir()
        (data / 'notify-forward.json').write_text(json.dumps([str(executable)]))
        payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': str(uuid.uuid4())})
        result = self.cli('notify', payload)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(output.read_text()), json.loads(payload))

    def test_session_start_does_not_enrol_an_unregistered_chat(self):
        self.enable_queue_fixture()
        event = {'hook_event_name': 'SessionStart', 'session_id': self.owner_thread,
                 'cwd': str(self.base), 'permission_mode': 'bypassPermissions'}
        self.env.pop('CODEX_THREAD_ID')
        result = self.cli('hook', input=json.dumps(event))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.cli('chats').stdout.strip(), '')
        self.assertEqual(self.cli('inboxes').stdout.strip(), '')

    def test_session_start_reconnects_only_an_already_registered_chat(self):
        self.enable_queue_fixture()
        registered = json.loads(self.cli('register', '--name', 'Powder planning').stdout)
        self.inboxes.add(registered['inbox'])
        os.kill(json.loads(self.cli('inboxes').stdout)['pid'], signal.SIGKILL)
        event = {'hook_event_name': 'SessionStart', 'session_id': self.owner_thread,
                 'cwd': str(self.base), 'permission_mode': 'bypassPermissions'}
        self.env.pop('CODEX_THREAD_ID')
        result = self.cli('hook', input=json.dumps(event))
        self.env['CODEX_THREAD_ID'] = self.owner_thread
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        chats = [json.loads(line) for line in self.cli('chats').stdout.splitlines()]
        self.assertEqual(len(chats), 1)
        state = json.loads(self.cli('inboxes').stdout)
        self.inboxes.add(state['inbox'])
        self.assertTrue(chats[0]['receiver_wakes_agent'])
        self.assertEqual(chats[0]['cwd'], str(self.base))
        self.assertEqual(chats[0]['reply_address'], registered['reply_address'])
        process, frame, sent = self.start_send(wait='0')
        process.communicate(timeout=5)
        self.assertEqual(frame['from_mode'], 'bypass')
        self.assertEqual(sent['reply_address'], chats[0]['reply_address'])

    def test_session_start_does_not_reopen_an_explicitly_closed_connection(self):
        self.enable_queue_fixture()
        registered = json.loads(self.cli('register').stdout)
        self.inboxes.add(registered['inbox'])
        self.assertEqual(self.cli('close', '--inbox', registered['inbox']).returncode, 0)
        event = {'hook_event_name': 'SessionStart', 'session_id': self.owner_thread,
                 'cwd': str(self.base), 'permission_mode': 'default'}
        self.assertEqual(self.cli('hook', input=json.dumps(event)).returncode, 0)
        self.assertFalse(json.loads(self.cli('inboxes').stdout)['open'])

    def test_legacy_automatic_registration_is_hidden_until_explicitly_registered(self):
        self.enable_queue_fixture()
        registered = json.loads(self.cli('register', '--sender-mode', 'prompting').stdout)
        self.inboxes.add(registered['inbox'])
        path = self.base / 'data' / 'chats' / f'{self.owner_thread}.json'
        legacy = json.loads(path.read_text())
        legacy.pop('participating')
        path.write_text(json.dumps(legacy))
        self.assertEqual(self.cli('chats', '--all').stdout.strip(), '')
        restored = json.loads(self.cli('register', '--name', 'Powder planning').stdout)
        self.assertEqual(restored['reply_address'], registered['reply_address'])
        rows = [json.loads(line) for line in self.cli('chats', '--all').stdout.splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['sender_mode'], 'prompting')
        self.assertEqual(rows[0]['chat_id'], self.owner_thread[-12:])

    def test_tracked_idle_answers_immediately_without_waking_the_codex_chat(self):
        self.enable_queue_fixture()
        registered = json.loads(self.cli('register').stdout)
        self.inboxes.add(registered['inbox'])
        self.cli('hook', input=json.dumps({'hook_event_name': 'UserPromptSubmit',
                                          'session_id': self.owner_thread, 'turn_id': 'tracked'}))
        self.cli('notify', json.dumps({'type': 'agent-turn-complete',
                                      'thread-id': self.owner_thread, 'turn-id': 'tracked'}))
        subscription = {'type': 'control', 'action': 'notify_when_idle', 'msgV': 1,
                        'msg_id': str(uuid.uuid4()), 'from': 'uds:' + str(self.socket_path)}
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(registered['reply_address'].removeprefix('uds:'))
            client.sendall((json.dumps(subscription) + '\n').encode())
        conn, _ = self.server.accept()
        with conn:
            notice = json.loads(conn.recv(65536))
        self.assertEqual(notice['orig_msg_id'], subscription['msg_id'])
        self.assertEqual(notice['state'], 'idle')
        self.assertFalse(self.queue_log.exists())

    def test_receiver_queues_later_reply_to_owner_without_acknowledging(self):
        self.enable_queue_fixture()
        process, frame, sent = self.start_send(wait='0')
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        self.reply(frame['from'], 'The slow build is ready for review.')

        queued = self.queued()
        self.assertEqual(queued[0][:3], ['queue', '--thread', self.owner_thread])
        self.assertEqual(queued[0][3], '--message')
        self.assertIn(f"{sent['inbox']}:1", queued[0][4])
        state = json.loads(self.cli('inboxes').stdout)
        self.assertTrue(sent['receiver_wakes_agent'])
        self.assertEqual(state['unread_count'], 1)
        self.assertEqual(state['acknowledged_cursor'], 0)

    def test_replayed_frame_is_saved_and_dispatched_once(self):
        self.enable_queue_fixture()
        process, frame, sent = self.start_send(wait='0')
        process.communicate(timeout=5)
        message_id = str(uuid.uuid4())
        for _ in range(2):
            self.reply(frame['from'], 'Trent finished the review.', message_id=message_id)
        self.reply(frame['from'], 'Fat Mike has another result.')
        queued = self.queued()
        rows = self.read_messages(sent['inbox'])
        self.assertEqual(len([row for row in rows if row['event'] == 'received']), 2)
        self.assertEqual(len(queued), 1)
        self.assertIn(f"{sent['inbox']}:2", queued[0][4])

    def test_interrupted_queue_attempt_stays_unread_and_is_not_retried(self):
        self.enable_queue_fixture()
        self.queue_log.with_suffix('.crash').touch()
        process, frame, sent = self.start_send(wait='0')
        process.communicate(timeout=5)
        self.reply(frame['from'], 'Greg Graffin finished the review.')
        self.queued()
        state = self.wait_dispatch_status('uncertain')
        self.assertFalse(state['receiver_wakes_agent'])
        self.assertEqual(state['unread_count'], 1)
        result = self.cli('dispatch', '--inbox', sent['inbox'])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(len(self.queued()), 1)

        rows = self.read_messages(sent['inbox'])
        self.assertEqual(rows[0]['frame']['message']['content'],
                         'Greg Graffin finished the review.')
        self.cli('ack', '--inbox', sent['inbox'], '--through', '1')
        result = self.cli('dispatch', '--inbox', sent['inbox'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.reply(frame['from'], 'Jane Doe finished a second review.')
        queued = self.queued(count=2)
        self.assertIn(f"{sent['inbox']}:2", queued[1][4])

    def test_blocked_queue_does_not_block_receiving_or_explicit_close(self):
        self.enable_queue_fixture()
        self.queue_log.with_suffix('.block').touch()
        process, frame, sent = self.start_send(wait='0')
        process.communicate(timeout=5)
        self.reply(frame['from'], 'First reply starts a slow queue command.')
        self.queued()
        self.reply(frame['from'], 'Second reply still reaches the saved inbox.')
        rows = self.read_messages(sent['inbox'], after=1)
        self.assertEqual(rows[0]['frame']['message']['content'],
                         'Second reply still reaches the saved inbox.')
        result = self.cli('close', '--inbox', sent['inbox'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(json.loads(result.stdout)['receiver_wakes_agent'])
        state = self.wait_dispatch_status('closed')
        self.assertEqual(state['unread_count'], 2)
        self.inboxes.discard(sent['inbox'])

    def test_notification_only_points_to_inbox_and_preserves_full_reply(self):
        self.enable_queue_fixture()
        process, frame, sent = self.start_send(wait='0')
        process.communicate(timeout=5)
        content = 'Powder day review. ' * 4000
        self.reply(frame['from'], content)
        queued = self.queued()
        notification = queued[0][4]
        self.assertLessEqual(len(notification.encode()), 600)
        self.assertIn(f"{sent['inbox']}:1", notification)
        self.assertNotIn('Powder day review.', notification)
        self.assertNotIn('"frame"', notification)
        rows = self.read_messages(sent['inbox'])
        self.assertEqual(rows[0]['frame']['message']['content'], content)

    def test_existing_storage_inbox_dispatches_backlog_only_to_its_owner(self):
        self.enable_queue_fixture()
        process, frame, sent = self.start_send(wait='0', no_dispatch=True)
        process.communicate(timeout=5)
        self.reply(frame['from'], 'John Doe has a saved handoff.')
        self.read_messages(sent['inbox'])
        self.assertFalse(sent['receiver_wakes_agent'])

        self.env['CODEX_THREAD_ID'] = str(uuid.uuid4())
        result = self.cli('dispatch', '--inbox', sent['inbox'])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse(self.queue_log.exists())
        self.env['CODEX_THREAD_ID'] = self.owner_thread
        for _ in range(2):
            result = self.cli('dispatch', '--inbox', sent['inbox'])
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(json.loads(self.cli('inboxes').stdout)['unread_count'], 1)

    def test_queue_failure_is_reported_without_consuming_or_resending_reply(self):
        self.enable_queue_fixture()
        self.queue_log.with_suffix('.fail').touch()
        process, frame, sent = self.start_send(wait='0')
        process.communicate(timeout=5)
        self.reply(frame['from'], 'The result must survive a queue failure.')
        state = self.wait_dispatch_status('error')
        self.assertIn('Queue is unavailable', state['dispatch_error'])
        self.assertFalse(state['receiver_wakes_agent'])
        self.assertEqual(state['unread_count'], 1)
        self.assertEqual(state['acknowledged_cursor'], 0)
        result = self.cli('dispatch', '--inbox', sent['inbox'])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(len(self.queued()), 1)

    def test_control_frames_are_saved_without_starting_codex_turns(self):
        self.enable_queue_fixture()
        process, frame, sent = self.start_send(wait='0')
        process.communicate(timeout=5)
        self.reply(frame['from'], 'Idle subscription.', frame_type='notify_when_idle')
        self.read_messages(sent['inbox'])
        self.assertFalse(self.queue_log.exists())
        self.reply(frame['from'], 'A real peer reply needs attention.')
        queued = self.queued()
        self.assertEqual(len(queued), 1)
        self.assertIn(f"{sent['inbox']}:2", queued[0][4])

    def test_dispatch_restart_does_not_repeat_a_queued_unread_reply(self):
        self.enable_queue_fixture()
        process, frame, sent = self.start_send(wait='0')
        process.communicate(timeout=5)
        self.reply(frame['from'], 'A queued handoff has not been handled yet.')
        self.queued()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = json.loads(self.cli('inboxes').stdout)
            if state['dispatch_cursor'] == 1:
                break
        self.assertEqual(state['dispatch_cursor'], 1)
        os.killpg(state['dispatch_pid'], signal.SIGTERM)
        self.wait_dispatch_status('stopped')
        result = self.cli('dispatch', '--inbox', sent['inbox'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.reply(frame['from'], 'A fresh handoff follows the restart.')
        queued = self.queued(count=2)
        self.assertEqual(len(queued), 2)
        self.assertIn(f"{sent['inbox']}:2", queued[1][4])
        self.assertEqual(json.loads(self.cli('inboxes').stdout)['unread_count'], 2)

    def test_explicit_dispatch_restart_preserves_address_and_handling_cursor(self):
        self.enable_queue_fixture()
        process, frame, sent = self.start_send(wait='0')
        process.communicate(timeout=5)
        self.reply(frame['from'], 'Trent finished the first review.')
        self.queued()
        self.cli('ack', '--inbox', sent['inbox'], '--through', '1')
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            before = json.loads(self.cli('inboxes').stdout)
            if before['dispatch_cursor'] == 1:
                break
            time.sleep(0.02)
        self.assertEqual(before['dispatch_cursor'], 1)

        result = self.cli('dispatch', '--inbox', sent['inbox'], '--restart')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = json.loads(self.cli('inboxes').stdout)
        self.assertNotEqual(after['dispatch_pid'], before['dispatch_pid'])
        self.assertEqual(after['reply_address'], sent['reply_address'])
        self.assertEqual(after['acknowledged_cursor'], 1)
        self.assertEqual(after['dispatch_cursor'], 1)
        self.assertTrue(after['open'])
        self.reply(frame['from'], 'Fat Mike finished the next review.')
        queued = self.queued(count=2)
        self.assertEqual(len(queued), 2)
        self.assertIn(f"{sent['inbox']}:2", queued[1][4])
        rows = self.read_messages(sent['inbox'], after=1)
        self.assertEqual(rows[0]['frame']['message']['content'],
                         'Fat Mike finished the next review.')

    def test_reply_arrives_after_send_command_times_out_and_exits(self):
        process, frame, sent = self.start_send()
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 3)
        self.assertTrue(Path(frame['from'][4:]).exists(),
                        'The reply socket disappeared when the foreground wait expired')
        self.reply(frame['from'], 'The slow build is finished.')
        rows = self.read_messages(sent['inbox'])
        replies = [r for r in rows if r['event'] == 'received']
        self.assertEqual(replies[0]['frame']['message']['content'], 'The slow build is finished.')
        self.assertEqual(rows[-1]['event'], 'read_complete')
        result = self.cli('close', '--inbox', sent['inbox'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(Path(frame['from'][4:]).exists())
        self.inboxes.discard(sent['inbox'])

    def test_acknowledgement_does_not_close_inbox_and_followup_reuses_address(self):
        process, frame, sent = self.start_send(wait='2')
        self.reply(frame['from'], 'Working on it.')
        output, errors = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, output + errors)
        initial = [json.loads(line) for line in output.splitlines()]
        cursor = initial[-1]['seq']
        self.assertTrue(Path(frame['from'][4:]).exists())

        followup, second_frame, second_sent = self.start_send(wait='0', inbox=sent['inbox'])
        followup.communicate(timeout=5)
        self.assertEqual(followup.returncode, 0)
        self.assertEqual(second_frame['from'], frame['from'])
        self.assertEqual(second_sent['inbox'], sent['inbox'])
        self.reply(frame['from'], 'The completed result is ready.')
        rows = self.read_messages(sent['inbox'], after=cursor)
        replies = [row for row in rows if row['event'] == 'received']
        self.assertEqual([row['frame']['message']['content'] for row in replies],
                         ['The completed result is ready.'])
        self.assertGreater(rows[-1]['cursor'], cursor)
        self.assertTrue(rows[-1]['open'])

    def test_followup_send_does_not_hide_the_unread_backlog(self):
        process, frame, sent = self.start_send(wait='0')
        process.communicate(timeout=5)
        self.reply(frame['from'], 'The handoff arrived while Codex was idle.')
        self.read_messages(sent['inbox'])

        followup, _, second_sent = self.start_send(wait='0', inbox=sent['inbox'])
        followup.communicate(timeout=5)
        self.assertEqual(second_sent['cursor'], 1)
        state = json.loads(self.cli('inboxes').stdout)
        self.assertEqual(state['acknowledged_cursor'], 0)
        self.assertEqual(state['unread_count'], 1)
        self.assertFalse(second_sent['receiver_wakes_agent'])
        unread = self.cli('read', '--inbox', sent['inbox'], '--unread')
        rows = [json.loads(line) for line in unread.stdout.splitlines()]
        self.assertEqual(rows[0]['frame']['message']['content'],
                         'The handoff arrived while Codex was idle.')
        self.assertTrue(rows[-1]['open'])

    def test_cancelling_foreground_wait_preserves_reply_inbox(self):
        process, frame, sent = self.start_send(wait='5')
        process.send_signal(signal.SIGINT)
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 130)
        self.reply(frame['from'], 'Reply after the caller stopped waiting.')
        rows = self.read_messages(sent['inbox'])
        self.assertEqual(rows[0]['frame']['message']['content'],
                         'Reply after the caller stopped waiting.')

    def test_explicit_close_preserves_saved_replies(self):
        process, frame, sent = self.start_send(wait='2')
        self.reply(frame['from'], 'Keep this reply after closing.')
        process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0)
        result = self.cli('close', '--inbox', sent['inbox'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(Path(frame['from'][4:]).exists())
        rows = self.read_messages(sent['inbox'])
        self.assertEqual(rows[0]['frame']['message']['content'], 'Keep this reply after closing.')
        self.assertFalse(rows[-1]['open'])
        self.inboxes.discard(sent['inbox'])

    def test_unread_replies_survive_reads_until_explicitly_acknowledged(self):
        process, frame, sent = self.start_send(wait='0')
        process.communicate(timeout=5)
        self.reply(frame['from'], 'The build needs your review.')
        delivered = self.read_messages(sent['inbox'])
        cursor = delivered[-1]['cursor']

        for _ in range(2):
            result = self.cli('read', '--inbox', sent['inbox'], '--unread')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            rows = [json.loads(line) for line in result.stdout.splitlines()]
            self.assertEqual(rows[0]['frame']['message']['content'],
                             'The build needs your review.')

        acknowledged = self.cli('ack', '--inbox', sent['inbox'], '--through', str(cursor))
        self.assertEqual(acknowledged.returncode, 0, acknowledged.stdout + acknowledged.stderr)
        result = self.cli('read', '--inbox', sent['inbox'], '--unread')
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([row['event'] for row in rows], ['read_complete'])
        self.assertEqual(rows[0]['acknowledged_cursor'], cursor)

        self.reply(frame['from'], 'A later handoff also needs attention.')
        result = self.cli('read', '--inbox', sent['inbox'], '--unread', '--wait', '1')
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(rows[0]['frame']['message']['content'],
                         'A later handoff also needs attention.')
        self.assertGreater(rows[-1]['cursor'], cursor)

    def test_acknowledgement_cannot_skip_future_messages_or_move_backwards(self):
        process, frame, sent = self.start_send(wait='0')
        process.communicate(timeout=5)
        self.reply(frame['from'], 'First review result.')
        self.read_messages(sent['inbox'])
        result = self.cli('ack', '--inbox', sent['inbox'], '--through', '2')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(self.cli('inboxes').stdout)['acknowledged_cursor'], 0)
        result = self.cli('ack', '--inbox', sent['inbox'], '--through', '1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.cli('ack', '--inbox', sent['inbox'], '--through', '0')
        self.assertEqual(json.loads(result.stdout)['cursor'], 1)

        self.reply(frame['from'], 'Second review result.')
        self.read_messages(sent['inbox'], after=1)
        result = self.cli('close', '--inbox', sent['inbox'])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.cli('read', '--inbox', sent['inbox'], '--unread')
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(rows[0]['frame']['message']['content'], 'Second review result.')
        self.assertEqual(rows[-1]['acknowledged_cursor'], 1)
        self.assertFalse(rows[-1]['open'])
        self.inboxes.discard(sent['inbox'])


    def test_reply_socket_is_named_for_the_inbox_outside_the_target_directory(self):
        before = set(self.base.glob('*.sock'))
        process, frame, sent = self.start_send()
        process.communicate(timeout=5)
        reply = Path(frame['from'].removeprefix('uds:'))
        self.assertEqual(reply.name, f"{sent['inbox']}.sock",
                         'The reply socket must be named for its inbox, not a reusable PID')
        self.assertNotEqual(reply.parent.resolve(), self.socket_path.parent.resolve(),
                            'The reply socket must not be created in the target socket directory')
        self.assertEqual(set(self.base.glob('*.sock')) - before, set(),
                         'Sending must not leave sockets in the target socket directory')

    def test_reply_socket_fits_the_address_limit_under_a_long_data_directory(self):
        nested = self.base / 'data' / ('deeply-nested-' + 'x' * 80)
        self.env = {**self.env, 'CLAUDE_PEER_DATA_DIR': str(nested)}
        self.assertGreater(len(str(nested).encode()), 103)
        process, frame, sent = self.start_send()
        process.communicate(timeout=5)
        reply = frame['from'].removeprefix('uds:')
        self.assertLessEqual(len(reply.encode()), 103, 'Reply address exceeds the AF_UNIX limit')
        self.reply(frame['from'], 'Long data directories still receive replies.')
        rows = self.read_messages(sent['inbox'])
        self.assertEqual(rows[0]['frame']['message']['content'],
                         'Long data directories still receive replies.')


    def legacy_inbox(self):
        """An inbox as it existed before reply sockets moved out of Claude's socket directory."""
        inbox = str(uuid.uuid4())
        directory = Path(self.env['CLAUDE_PEER_DATA_DIR']) / 'inboxes' / inbox
        directory.mkdir(parents=True, exist_ok=True)
        legacy = self.base / f'legacy-{inbox[:8]}.sock'
        receiver = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        receiver.bind(str(legacy))
        legacy.chmod(0o600)
        receiver.listen(4)
        self.addCleanup(receiver.close)
        (directory / 'state.json').write_text(json.dumps({
            'inbox': inbox, 'status': 'open', 'created_at': '2026-09-29T00:00:00-07:00',
            'socket_directory': str(self.base), 'owner_thread': None,
            'pid': os.getpid(), 'socket_path': str(legacy),
            'reply_address': 'uds:' + str(legacy), 'log': str(directory / 'messages.jsonl'),
        }))
        return inbox, legacy

    def test_inbox_created_before_the_socket_move_keeps_its_return_address(self):
        inbox, legacy = self.legacy_inbox()
        process, frame, sent = self.start_send(inbox=inbox)
        output, errors = process.communicate(timeout=5)
        self.assertEqual(sent['inbox'], inbox, output + errors)
        self.assertEqual(sent['reply_address'], 'uds:' + str(legacy),
                         'Reusing a pre-move inbox must preserve its original return address')
        self.assertEqual(frame['from'], 'uds:' + str(legacy))
        self.inboxes.discard(inbox)


if __name__ == '__main__':
    unittest.main()
