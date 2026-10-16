"""Exercise the MCP interface through the official SDK's stdio client."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
import uuid

from mcp import Client
from mcp.client.stdio import StdioServerParameters

DIRECTORY = Path(__file__).parent


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_discovery_registration_read_and_ack_use_real_stdio_tools(self):
        await self.exercise_stdio('auto')

    async def test_tools_also_work_with_legacy_mcp_clients(self):
        await self.exercise_stdio('legacy')

    async def exercise_stdio(self, mode):
        with tempfile.TemporaryDirectory(dir=Path.home() / '.claude-scratchpad') as directory:
            owner = str(uuid.uuid4())
            env = {**os.environ, 'CODEX_THREAD_ID': owner,
                   'PATH': directory,
                   'CLAUDE_CONFIG_DIR': directory,
                   'CLAUDE_PEER_DATA_DIR': str(Path(directory) / 'data')}
            inbox = None
            try:
                async with Client(StdioServerParameters(
                    command=sys.executable, args=[str(DIRECTORY / 'claude_peer_mcp.py')], env=env,
                ), mode=mode) as client:
                    tools = await client.list_tools()
                    self.assertIn('register_chat', [tool.name for tool in tools.tools])
                    result = await client.call_tool('register_chat', {
                        'thread_id': owner, 'name': 'MCP powder review',
                        'cwd': directory, 'dispatch': False,
                    })
                    self.assertFalse(result.is_error)
                    self.assertIsNotNone(result.structured_content)
                    registration = result.structured_content
                    inbox = registration['inbox']
                    target = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    target_path = Path(directory) / 'claude.sock'
                    target.bind(str(target_path))
                    target_path.chmod(0o600)
                    target.listen(1)
                    target.settimeout(5)
                    registry = Path(directory) / 'sessions'
                    registry.mkdir()
                    (registry / f'{os.getpid()}.json').write_text(json.dumps({
                        'pid': os.getpid(), 'sessionId': str(uuid.uuid4()),
                        'messagingSocketPath': str(target_path), 'name': 'Claude fixture',
                        'cwd': directory, 'status': 'idle',
                    }))
                    try:
                        text = '--literal message\nwith a second line'
                        sending = asyncio.create_task(client.call_tool('send_message', {
                            'thread_id': owner, 'pid': os.getpid(), 'message': text,
                        }))
                        conn, _ = await asyncio.to_thread(target.accept)
                        with conn:
                            outgoing = json.loads(conn.recv(65536))
                        sent = await sending
                        self.assertFalse(sent.is_error)
                        self.assertIn(text, outgoing['message']['content'])
                        self.assertEqual(sent.structured_content['inbox'], inbox)
                    finally:
                        target.close()
                    resumed = subprocess.run(
                        [sys.executable, str(DIRECTORY / 'claude_peer.py'), 'hook'], env=env,
                        input=json.dumps({'hook_event_name': 'SessionStart', 'session_id': owner,
                                          'cwd': directory}), capture_output=True, text=True,
                    )
                    self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)
                    peers = await client.call_tool('list_peers', {})
                    self.assertEqual((peers.structured_content or json.loads(peers.content[0].text))['codex_chats'][0]['name'],
                                     'MCP powder review')
                    self.assertFalse(peers.structured_content['codex_chats'][0]['receiver_wakes_agent'])
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sender:
                        sender.connect(registration['reply_address'].removeprefix('uds:'))
                        sender.sendall((json.dumps({'type': 'user', 'from': 'fixture',
                                                   'msg_id': str(uuid.uuid4()),
                                                   'message': {'content': 'Review ready.'}}) + '\n').encode())
                    result = await client.call_tool('read_inbox', {
                        'thread_id': owner, 'inbox': inbox, 'wait': 1,
                    })
                    self.assertEqual((result.structured_content or json.loads(result.content[0].text))['records'][0]['seq'], 1)
                    result = await client.call_tool('acknowledge', {
                        'thread_id': owner, 'inbox': inbox, 'through': 1,
                    })
                    self.assertEqual((result.structured_content or json.loads(result.content[0].text))['cursor'], 1)
                    result = await client.call_tool('read_inbox', {'thread_id': owner, 'inbox': inbox})
                    self.assertEqual(len((result.structured_content or json.loads(result.content[0].text))['records']), 1)
                    wrong = await client.call_tool('acknowledge', {
                        'thread_id': str(uuid.uuid4()), 'inbox': inbox, 'through': 1,
                    })
                    self.assertTrue(wrong.is_error)
            finally:
                for saved in (Path(directory) / 'data' / 'inboxes').glob('*/state.json'):
                    saved_inbox = json.loads(saved.read_text())['inbox']
                    subprocess.run([sys.executable, str(DIRECTORY / 'claude_peer.py'),
                                    'close', '--inbox', saved_inbox], env=env, capture_output=True)



if __name__ == '__main__':
    unittest.main()
