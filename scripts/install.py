#!/usr/bin/env python3
"""Install the local MCP interface, completion notifier, hooks, and Claude discovery skill."""
import argparse
import json
import os
from pathlib import Path
import shutil
import shlex
import subprocess
import sys
import tomllib

import claude_peer as peer

ROOT = Path(__file__).resolve().parent.parent


def is_bridge_hook(handler):
    if handler.get('type') != 'command':
        return False
    try:
        parts = shlex.split(handler.get('command', ''))
    except ValueError:
        return False
    return len(parts) == 3 and parts[1:] == [str(ROOT / 'scripts' / 'claude_peer.py'), 'hook']


def replace_notify(source, value):
    lines = source.splitlines(keepends=True)
    replacement = 'notify = ' + json.dumps(value) + '\n'
    for first, line in enumerate(lines):
        if line.lstrip().startswith('['):
            break
        if line.lstrip().startswith('notify') and line.split('=', 1)[0].strip() == 'notify':
            for last in range(first + 1, len(lines) + 1):
                try:
                    parsed = tomllib.loads(''.join(lines[first:last]))
                except tomllib.TOMLDecodeError:
                    continue
                if 'notify' in parsed:
                    return ''.join(lines[:first]) + replacement + ''.join(lines[last:])
            raise ValueError('Cannot safely replace the existing notify assignment')
    return replacement + source


def install(codex_home, claude_home):
    codex_home.mkdir(parents=True, exist_ok=True)
    config_path = codex_home / 'config.toml'
    source = config_path.read_text() if config_path.exists() else ''
    config = tomllib.loads(source)
    command = [sys.executable, str(ROOT / 'scripts' / 'claude_peer.py'), 'notify']
    old = config.get('notify', [])
    peer.OUTPUT.mkdir(parents=True, mode=0o700, exist_ok=True)
    is_bridge = (len(old) >= 3 and isinstance(old[1], str) and
                 old[1] == str(ROOT / 'scripts' / 'claude_peer.py') and old[2] == 'notify')
    if old != command:
        if not isinstance(old, list) or not all(isinstance(part, str) for part in old):
            raise ValueError('Existing notify command is not an argument list')
        if not is_bridge:
            peer.write_json(peer.OUTPUT / 'notify-forward.json', old)
        config_path.write_text(replace_notify(source, command))
    hooks_path = codex_home / 'hooks.json'
    hooks_config = json.loads(hooks_path.read_text()) if hooks_path.exists() else {}
    hooks = hooks_config.setdefault('hooks', {})
    hook_command = ' '.join(shlex.quote(part) for part in
                            [sys.executable, str(ROOT / 'scripts' / 'claude_peer.py'), 'hook'])
    for event in ('SessionStart', 'UserPromptSubmit', 'Interrupt', 'SessionEnd'):
        groups = hooks.setdefault(event, [])
        installed = [handler for group in groups for handler in group.get('hooks', [])
                     if is_bridge_hook(handler)]
        timeout = 10 if event == 'SessionStart' else 3
        if installed:
            for handler in installed:
                handler['command'] = hook_command
                handler['timeout'] = timeout
        else:
            groups.append({'hooks': [{'type': 'command', 'command': hook_command, 'timeout': timeout}]})
    peer.write_json(hooks_path, hooks_config)
    environment = {**os.environ, 'CODEX_HOME': str(codex_home)}
    subprocess.run(['codex', 'mcp', 'add', 'claude_code_messaging', '--',
                    shutil.which('uv') or 'uv', 'run', '--script',
                    str(ROOT / 'scripts' / 'claude_peer_mcp.py')],
                   env=environment, check=True, capture_output=True, text=True)
    destination = claude_home / 'skills' / 'codex-chat-messaging'
    destination.mkdir(parents=True, exist_ok=True)
    template = (ROOT / 'claude-skill' / 'SKILL.md').read_text()
    (destination / 'SKILL.md').write_text(template.replace('{{HELPER}}', str(ROOT / 'scripts' / 'claude_peer.py')))
    print(json.dumps({'mcp': 'claude_code_messaging', 'notify_forward_preserved': True,
                      'claude_skill': str(destination / 'SKILL.md'),
                      'hooks_require_review': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--codex-home', type=Path,
                        default=Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex'))
    parser.add_argument('--claude-home', type=Path,
                        default=Path(os.environ.get('CLAUDE_CONFIG_DIR') or Path.home() / '.claude'))
    args = parser.parse_args()
    install(args.codex_home.expanduser(), args.claude_home.expanduser())
