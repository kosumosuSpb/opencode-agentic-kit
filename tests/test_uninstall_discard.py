"""Integration regression for explicit uninstall of unverified original backups."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / 'install.py'


class UninstallDiscardBackupsTest(unittest.TestCase):
    def test_unverified_originals_require_explicit_discard(self) -> None:
        with tempfile.TemporaryDirectory(prefix='agentic-discard-regression-') as directory:
            lab = Path(directory)
            config = lab / 'config' / 'opencode'
            agents = config / 'agents'
            agents.mkdir(parents=True)

            original_agents = {
                'architect.md': b'previous architect\n',
                'developer.md': b'previous developer\n',
                'reviewer.md': b'previous reviewer\n',
            }
            for filename, content in original_agents.items():
                (agents / filename).write_bytes(content)
            cli_path = config / 'cli.json'
            cli_path.write_text(json.dumps({'user_setting': 'keep-me'}), encoding='utf-8')

            env = os.environ.copy()
            env.pop('AGENTIC_RUNNER', None)
            env.update(
                HOME=str(lab / 'home'),
                XDG_CONFIG_HOME=str(lab / 'config'),
                XDG_DATA_HOME=str(lab / 'data'),
                XDG_STATE_HOME=str(lab / 'state'),
                XDG_CACHE_HOME=str(lab / 'cache'),
                AGENTIC_STATE_DIR=str(lab / 'state' / 'opencode-agentic'),
            )
            (lab / 'home').mkdir()

            def installer(*arguments: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    [sys.executable, str(INSTALLER), *arguments],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            installed = installer('--install')
            self.assertEqual(installed.returncode, 0, installed.stderr)

            manifest = lab / 'state' / 'opencode-agentic' / 'install' / 'manifest.json'
            data = json.loads(manifest.read_text(encoding='utf-8'))
            discarded = set()
            for entry in [*data['files'], data.get('tui_config')]:
                if entry is None or not entry.get('existed'):
                    continue
                filename = Path(entry['dest']).name
                if filename in {'architect.md', 'developer.md', 'cli.json'}:
                    self.assertIsInstance(entry.get('backup_hash'), str)
                    entry.pop('backup_hash')
                    discarded.add(filename)
            self.assertEqual(discarded, {'architect.md', 'developer.md', 'cli.json'})
            manifest.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')

            installed_architect = (agents / 'architect.md').read_bytes()
            ordinary = installer('--uninstall')
            self.assertNotEqual(ordinary.returncode, 0)
            self.assertIn('UNVERIFIED_BACKUP', ordinary.stderr)
            self.assertTrue(manifest.is_file())
            self.assertEqual((agents / 'architect.md').read_bytes(), installed_architect)

            discard = installer('--uninstall', '--discard-backups')
            self.assertEqual(discard.returncode, 0, discard.stderr)
            self.assertIn('UNINSTALLED:', discard.stdout)
            self.assertFalse((agents / 'architect.md').exists())
            self.assertFalse((agents / 'developer.md').exists())
            self.assertEqual((agents / 'reviewer.md').read_bytes(), original_agents['reviewer.md'])
            self.assertEqual(
                json.loads(cli_path.read_text(encoding='utf-8'))['user_setting'], 'keep-me'
            )
            self.assertFalse((config / 'plugins' / 'agentic-kit' / 'index.ts').exists())
            self.assertFalse(manifest.exists())

            repeat = installer('--uninstall')
            self.assertEqual(repeat.returncode, 0, repeat.stderr)
            self.assertIn('NOT_INSTALLED', repeat.stdout)


if __name__ == '__main__':
    unittest.main()
