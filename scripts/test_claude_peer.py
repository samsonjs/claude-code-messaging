"""Exercise reply lifetimes through the bridge CLI and real local sockets."""
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
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
        self.inboxes = set()
        self.processes = []

    def tearDown(self):
        for inbox in self.inboxes:
            self.cli('close', '--inbox', inbox)
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)
        self.server.close()
        self.temp.cleanup()

    def cli(self, *args):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args], env=self.env,
            capture_output=True, text=True, timeout=10,
        )

    def start_send(self, wait='0.05', inbox=None):
        args = [
            sys.executable, '-u', str(SCRIPT), 'send', '--pid', str(os.getpid()),
            '--wait', wait, '--message', 'Please reply when the build finishes.',
        ]
        if inbox:
            args += ['--inbox', inbox]
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

    def reply(self, address, text):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(address.removeprefix('uds:'))
            client.sendall((json.dumps({
                'type': 'user', 'msgV': 1, 'msg_id': str(uuid.uuid4()),
                'from': 'uds:' + str(self.socket_path),
                'message': {'role': 'user', 'content': text},
            }) + '\n').encode())

    def read_messages(self, inbox, after=0):
        result = self.cli('read', '--inbox', inbox, '--after', str(after), '--wait', '1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines()]

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


if __name__ == '__main__':
    unittest.main()
