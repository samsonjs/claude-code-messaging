"""Check local installation without modifying the person's real configuration."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tomllib
import unittest

SCRIPT = Path(__file__).with_name('install.py')


class InstallTests(unittest.TestCase):
    def test_install_preserves_notifier_and_existing_hooks_and_is_repeatable(self):
        with tempfile.TemporaryDirectory(dir=Path.home() / '.claude-scratchpad') as directory:
            base = Path(directory)
            codex = base / 'codex-home'
            codex.mkdir()
            original = '["/opt/powder-notifier", "turn-ended"]'
            (codex / 'config.toml').write_text(f'notify = {original}\nmodel = "existing-model"\n')
            hook = {'type': 'command', 'command': '/opt/existing-hook'}
            (codex / 'hooks.json').write_text(json.dumps({'hooks': {'Stop': [{'hooks': [hook]}]}}))
            executable = base / 'codex'
            executable.write_text(f'#!{sys.executable}\nimport sys\nassert sys.argv[1:3] == ["mcp", "add"]\n')
            executable.chmod(0o700)
            env = {**os.environ, 'PATH': str(base) + os.pathsep + os.environ['PATH'],
                   'CLAUDE_PEER_DATA_DIR': str(base / 'data')}
            command = [sys.executable, str(SCRIPT), '--codex-home', str(codex),
                       '--claude-home', str(base / 'claude')]
            for _ in range(2):
                result = subprocess.run(command, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                if _ == 0:
                    source = (codex / 'config.toml').read_text()
                    command_line = source.splitlines()[0]
                    previous = tomllib.loads(source)['notify']
                    previous[0] = '/opt/previous-python'
                    (codex / 'config.toml').write_text(source.replace(
                        command_line, 'notify = ' + json.dumps(previous), 1))
            config = tomllib.loads((codex / 'config.toml').read_text())
            self.assertEqual(config['model'], 'existing-model')
            self.assertIn('notify', config['notify'])
            self.assertEqual(json.loads((base / 'data' / 'notify-forward.json').read_text()),
                             json.loads(original))
            hooks = json.loads((codex / 'hooks.json').read_text())['hooks']
            self.assertEqual(hooks['Stop'], [{'hooks': [hook]}])
            self.assertEqual(len(hooks['UserPromptSubmit']), 1)
            self.assertTrue((base / 'claude' / 'skills' / 'codex-chat-messaging' / 'SKILL.md').is_file())


if __name__ == '__main__':
    unittest.main()
