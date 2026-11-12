from __future__ import annotations
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

INSTALLER = Path(__file__).resolve().parents[1] / 'install.py'


class InstallerTests(unittest.TestCase):
    def test_dry_run_apply_idempotent_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {**os.environ, 'HOME': tmp,
                   'XDG_CONFIG_HOME': str(home / 'config'),
                   'XDG_DATA_HOME': str(home / 'data'),
                   'XDG_STATE_HOME': str(home / 'state')}
            arch = home / 'config/opencode/agents/architect.md'
            arch.parent.mkdir(parents=True)
            arch.write_text('my custom role\n')
            def run(*args):
                return subprocess.run([sys.executable, str(INSTALLER), *args],
                                      env=env, text=True, capture_output=True)
            dry = run()
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertEqual(arch.read_text(), 'my custom role\n')
            install = run('--install')
            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertIn('.agentic/drafts/spec.md', arch.read_text())
            same = run('--install')
            self.assertIn('ALREADY_INSTALLED', same.stdout)
            rollback = run('--rollback')
            self.assertEqual(rollback.returncode, 0, rollback.stderr)
            self.assertEqual(arch.read_text(), 'my custom role\n')
            plugin = home / 'config/opencode/plugins/agentic-preview/index.ts'
            self.assertFalse(plugin.exists())

    def test_upgrade_from_canonical_sources_preserves_original_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = home / 'canonical'
            source.mkdir()
            shutil.copy2(INSTALLER, source / 'install.py')
            root = INSTALLER.parent
            for rel in ('plugin/index.ts', 'agents/architect.md', 'agents/developer.md',
                        'agents/reviewer.md', 'runner/agentic_gate.py'):
                dest = source / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / rel, dest)
            env = {**os.environ, 'HOME': tmp,
                   'XDG_CONFIG_HOME': str(home / 'config'),
                   'XDG_DATA_HOME': str(home / 'data'),
                   'XDG_STATE_HOME': str(home / 'state')}
            arch = home / 'config/opencode/agents/architect.md'
            arch.parent.mkdir(parents=True)
            arch.write_text('original custom architect\n')

            def run(*args):
                return subprocess.run([sys.executable, str(source / 'install.py'), *args],
                                      env=env, text=True, capture_output=True)

            self.assertEqual(run('--install').returncode, 0)
            plugin = home / 'config/opencode/plugins/agentic-preview/index.ts'
            initial = plugin.read_text()
            manifest = home / 'state/opencode-agentic/preview-install/manifest.json'
            old = json.loads(manifest.read_text())
            old['version'] = '1.2-preview.1'
            for entry in old['files']:
                entry['source'] = entry['source'].replace('canonical', 'legacy-preview-source')
            manifest.write_text(json.dumps(old))
            (source / 'plugin/index.ts').write_text(initial + '\n// v1.2-preview.2 change\n')
            # A source change must never be silently considered installed.
            refused = run('--install')
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn('UPGRADE_REQUIRED', refused.stderr)
            self.assertEqual(plugin.read_text(), initial)
            upgraded = run('--upgrade')
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            self.assertIn('v1.2-preview.2 change', plugin.read_text())
            self.assertIn('ALREADY_INSTALLED', run('--install').stdout)
            self.assertEqual(run('--rollback').returncode, 0)
            self.assertEqual(arch.read_text(), 'original custom architect\n')
            self.assertFalse(plugin.exists())

    def test_upgrade_refuses_locally_modified_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {**os.environ, 'HOME': tmp,
                   'XDG_CONFIG_HOME': str(home / 'config'),
                   'XDG_DATA_HOME': str(home / 'data'),
                   'XDG_STATE_HOME': str(home / 'state')}

            def run(*args):
                return subprocess.run([sys.executable, str(INSTALLER), *args],
                                      env=env, text=True, capture_output=True)

            self.assertEqual(run('--install').returncode, 0)
            plugin = home / 'config/opencode/plugins/agentic-preview/index.ts'
            plugin.write_text('// local changes\n')
            result = run('--upgrade')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('LOCAL_CHANGE', result.stderr)
            self.assertEqual(plugin.read_text(), '// local changes\n')

    def test_modified_installed_files_not_overwritten_on_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {**os.environ, 'HOME': tmp,
                   'XDG_CONFIG_HOME': str(home / 'config'),
                   'XDG_DATA_HOME': str(home / 'data'),
                   'XDG_STATE_HOME': str(home / 'state')}
            def run(*args):
                return subprocess.run([sys.executable, str(INSTALLER), *args],
                                      env=env, text=True, capture_output=True)
            self.assertEqual(run('--install').returncode, 0)
            plugin = home / 'config/opencode/plugins/agentic-preview/index.ts'
            plugin.write_text('// local edit\n')
            rollback = run('--rollback')
            self.assertNotEqual(rollback.returncode, 0)
            self.assertEqual(plugin.read_text(), '// local edit\n')

    def test_installed_runner_survives_source_tree_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = home / 'canonical'
            source.mkdir()
            shutil.copy2(INSTALLER, source / 'install.py')
            root = INSTALLER.parent
            for rel in ('plugin/index.ts', 'agents/architect.md', 'agents/developer.md',
                        'agents/reviewer.md', 'runner/agentic_gate.py'):
                dest = source / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / rel, dest)
            env = {**os.environ, 'HOME': tmp,
                   'XDG_CONFIG_HOME': str(home / 'config'),
                   'XDG_DATA_HOME': str(home / 'data'),
                   'XDG_STATE_HOME': str(home / 'state')}
            install = subprocess.run([sys.executable, str(source / 'install.py'), '--install'],
                                     env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stderr)
            shutil.rmtree(source)

            project = home / 'project'
            project.mkdir()
            subprocess.run(['git', 'init', '-q', str(project)], check=True)
            runner = home / 'data/opencode-agentic/preview/runner/agentic_gate.py'
            result = subprocess.run([sys.executable, str(runner), '--project', str(project), 'fingerprint'],
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout.strip(), r'^[0-9a-f]{64}$')


if __name__ == '__main__':
    unittest.main()
