#!/usr/bin/env python3
"""Exchange messages with local Claude Code sessions using persistent reply inboxes."""
import argparse
import datetime
import json
import math
import os
from pathlib import Path
import signal
import socket
import stat
import subprocess
import sys
import time
import uuid

REGISTRY = Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude').expanduser() / 'sessions'
OUTPUT = Path(os.environ.get('CLAUDE_PEER_DATA_DIR') or
              Path.home() / '.claude-scratchpad' / 'claude-peer-bridge').expanduser()


def now():
    return datetime.datetime.now().astimezone().isoformat()


def emit(event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def append(path, event):
    with path.open('a') as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + '\n')


def write_state(directory, state):
    temporary = directory / f'state-{os.getpid()}.tmp'
    temporary.write_text(json.dumps(state, ensure_ascii=False) + '\n')
    temporary.replace(directory / 'state.json')


def inbox_directory(inbox):
    if str(uuid.UUID(inbox)) != inbox:
        raise ValueError('Inbox must be the UUID returned by send or inboxes')
    return OUTPUT / 'inboxes' / inbox


def inbox_state(inbox):
    return json.loads((inbox_directory(inbox) / 'state.json').read_text())


def inbox_open(state):
    if state.get('status') != 'open':
        return False
    try:
        os.kill(state['pid'], 0)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(0.2)
            client.connect(state['socket_path'])
        return True
    except OSError:
        return False


def sessions():
    for path in sorted(REGISTRY.glob('*.json')):
        try:
            entry = json.loads(path.read_text())
            os.kill(entry['pid'], 0)
            if Path(entry['messagingSocketPath']).is_socket():
                yield entry
        except (OSError, ValueError, KeyError):
            continue


def private_socket(path):
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) != 0o700:
        raise ValueError('Socket directory must be owned by this account and have mode 0700')
    info = path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError('Target must be a socket owned by this account')
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError('Target socket must have no group or other access')


def start_inbox(socket_directory):
    inbox = str(uuid.uuid4())
    directory = inbox_directory(inbox)
    directory.mkdir(parents=True, mode=0o700)
    write_state(directory, {
        'inbox': inbox, 'status': 'starting', 'created_at': now(),
        'socket_directory': str(socket_directory),
        'owner_thread': os.environ.get('CODEX_THREAD_ID'),
    })
    with (directory / 'receiver.stderr').open('a') as errors:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), '_serve', '--inbox', inbox],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors,
            start_new_session=True, close_fds=True,
        )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = inbox_state(inbox)
        if state['status'] == 'open':
            return state
        if state['status'] == 'error' or process.poll() is not None:
            raise ValueError(f'Inbox failed to start: {state.get("error", directory)}')
        time.sleep(0.02)
    (directory / 'stop').touch()
    raise ValueError(f'Inbox startup timed out: {inbox}')


def serve(inbox):
    directory = inbox_directory(inbox)
    state = inbox_state(inbox)
    path = Path(state['socket_directory']) / f'{os.getpid()}.sock'
    stop = directory / 'stop'
    log = directory / 'messages.jsonl'
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    bound = False
    sequence = 0

    def interrupted(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        listener.bind(str(path))
        bound = True
        path.chmod(0o600)
        listener.listen(16)
        listener.settimeout(0.2)
        state.update(status='open', pid=os.getpid(), socket_path=str(path),
                     reply_address='uds:' + str(path), log=str(log))
        write_state(directory, state)
        while not stop.exists():
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            with conn:
                conn.settimeout(0.2)
                buffer = b''
                total = 0
                deadline = time.monotonic() + 5
                while not stop.exists() and time.monotonic() < deadline and total < 1048576:
                    try:
                        chunk = conn.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    total += len(chunk)
                    buffer += chunk
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        try:
                            frame = json.loads(line)
                        except (ValueError, UnicodeDecodeError):
                            continue
                        if not isinstance(frame, dict) or frame.get('type') == 'auth':
                            continue
                        sequence += 1
                        append(log, {'at': now(), 'event': 'received', 'inbox': inbox,
                                     'seq': sequence, 'frame': frame})
    except KeyboardInterrupt:
        pass
    except Exception as error:
        state.update(status='error', error=str(error))
        raise
    finally:
        listener.close()
        if bound:
            path.unlink(missing_ok=True)
        if state['status'] != 'error':
            state['status'] = 'closed'
        state['closed_at'] = now()
        write_state(directory, state)
    return 0


def messages(inbox, after):
    path = inbox_directory(inbox) / 'messages.jsonl'
    if not path.exists():
        return []
    result = []
    with path.open() as handle:
        for line in handle:
            if not line.endswith('\n'):
                break
            row = json.loads(line)
            if row['seq'] > after:
                result.append(row)
    return result


def wait_messages(inbox, after, seconds):
    deadline = time.monotonic() + seconds
    while True:
        rows = messages(inbox, after)
        if rows or time.monotonic() >= deadline or not inbox_open(inbox_state(inbox)):
            return rows
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))


def send(args):
    target = next((s for s in sessions() if s['pid'] == args.pid), None)
    if target is None:
        raise ValueError(f'No live registered Claude session for PID {args.pid}')
    text = sys.stdin.read() if args.message == '-' else args.message
    if not text.strip():
        raise ValueError('Message must not be empty')
    path = Path(target['messagingSocketPath'])
    private_socket(path)
    # Reject oversized messages before starting a background receiver.
    if len(text.encode()) > 60000:
        raise ValueError('Message exceeds this bridge\'s 60 KiB text limit')
    state = inbox_state(args.inbox) if args.inbox else start_inbox(path.parent)
    if not inbox_open(state):
        raise ValueError('Reply inbox is closed or unavailable; send using a new inbox')
    if Path(state['socket_path']).parent.resolve() != path.parent.resolve():
        raise ValueError('Reply inbox belongs to a different socket directory; use a new inbox')
    inbox = state['inbox']
    address = state['reply_address']
    previous = messages(inbox, 0)
    cursor = previous[-1]['seq'] if previous else 0
    message_id = str(uuid.uuid4())
    log = OUTPUT / f'{message_id}.jsonl'

    def record(event):
        entry = {'at': now(), **event}
        append(log, entry)
        emit(entry)

    body = text.replace('</cross-session-message', '<\\/cross-session-message')
    content = (
        f'<cross-session-message from="{address}" from-name="Codex">\n'
        f'{body}\n</cross-session-message>'
    )
    frame = {
        'msgV': 1, 'msg_id': message_id, 'type': 'user',
        'message': {'role': 'user', 'content': content}, 'priority': 'next',
        'session_id': target['sessionId'], 'from': address,
    }
    wire = (json.dumps(frame, ensure_ascii=False) + '\n').encode()
    if len(wire) > 65536:
        raise ValueError('Encoded message exceeds this bridge\'s 64 KiB limit')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(5)
        client.connect(str(path))
        client.sendall(wire)
        client.shutdown(socket.SHUT_WR)
        while client.recv(4096):
            pass
    record({
        'event': 'sent', 'to': target['name'], 'pid': target['pid'], 'msg_id': message_id,
        'inbox': inbox, 'reply_address': address, 'cursor': cursor,
        'text': text, 'log': str(log), 'inbox_log': state['log'],
    })
    if not args.wait:
        return 0
    deadline = time.monotonic() + args.wait
    while time.monotonic() < deadline:
        rows = wait_messages(inbox, cursor, max(0, deadline - time.monotonic()))
        for row in rows:
            cursor = row['seq']
            record(row)
            incoming = row['frame']
            if incoming.get('from') != 'uds:' + str(path):
                continue
            if incoming.get('type') == 'user':
                return 0
            if incoming.get('orig_msg_id') == message_id:
                if incoming.get('status') in {'denied', 'expired', 'refused', 'dropped'}:
                    return 2
        if not inbox_open(inbox_state(inbox)):
            raise ValueError(f'Reply receiver stopped; saved messages remain in inbox {inbox}')
    record({'event': 'timeout', 'inbox': inbox, 'cursor': cursor,
            'detail': 'Foreground wait expired; inbox remains open. Use read to collect later replies.'})
    return 3


def read(args):
    inbox_state(args.inbox)
    rows = wait_messages(args.inbox, args.after, args.wait)
    for row in rows:
        emit(row)
    state = inbox_state(args.inbox)
    emit({'event': 'read_complete', 'inbox': args.inbox,
          'cursor': rows[-1]['seq'] if rows else args.after,
          'open': inbox_open(state), 'reply_address': state.get('reply_address')})
    return 0


def close(inbox):
    directory = inbox_directory(inbox)
    state = inbox_state(inbox)
    (directory / 'stop').touch()
    deadline = time.monotonic() + 6
    while state['status'] not in {'closed', 'error'} and inbox_open(state):
        if time.monotonic() >= deadline:
            raise ValueError('Receiver has not closed yet; stop requested, retry close to check')
        time.sleep(0.05)
        state = inbox_state(inbox)
    emit({'event': 'closed', 'inbox': inbox, 'log': state.get('log')})
    return 0


def duration(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError('Wait must be a finite non-negative number')
    return number


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('list', help='Discover running Claude Code sessions')
    sub.add_parser('inboxes', help='List saved reply inboxes')
    post = sub.add_parser('send', help='Send with a persistent return address')
    post.add_argument('--pid', type=int, required=True)
    post.add_argument('--message', required=True, help='Text, or - to read stdin')
    post.add_argument('--inbox', help='Reuse an inbox UUID returned by an earlier send')
    post.add_argument('--wait', type=duration, default=120,
                      help='Foreground wait in seconds; 0 returns immediately. Inbox stays open.')
    reader = sub.add_parser('read', help='Read replies without resending')
    reader.add_argument('--inbox', required=True)
    reader.add_argument('--after', type=int, default=0, help='Only messages after this cursor')
    reader.add_argument('--wait', type=duration, default=0, help='Seconds to await a new message')
    closer = sub.add_parser('close', help='Stop an inbox receiver; preserve saved messages')
    closer.add_argument('--inbox', required=True)
    receiver = sub.add_parser('_serve', help=argparse.SUPPRESS)
    receiver.add_argument('--inbox', required=True)
    args = parser.parse_args()
    if args.command == 'list':
        for entry in sessions():
            emit({k: entry.get(k) for k in
                  ('pid', 'name', 'cwd', 'status', 'sessionId', 'messagingSocketPath')})
        return 0
    if args.command == 'inboxes':
        for path in sorted((OUTPUT / 'inboxes').glob('*/state.json')):
            state = json.loads(path.read_text())
            emit({**state, 'open': inbox_open(state)})
        return 0
    if args.command == 'read':
        if args.after < 0:
            raise ValueError('--after must be non-negative')
        return read(args)
    if args.command == 'close':
        return close(args.inbox)
    if args.command == '_serve':
        return serve(args.inbox)
    OUTPUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    return send(args)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        emit({'event': 'error', 'detail': str(error)})
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
