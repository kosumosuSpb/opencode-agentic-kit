from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

INSTALLER = Path(__file__).absolute().parents[1] / 'install.py'


def load_installer_module():
    spec = importlib.util.spec_from_file_location('installer_under_test', INSTALLER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


INSTALLER_MODULE = load_installer_module()


def write_tagged_release(tag: str, destination: Path) -> Path:
    """Materialize the exact released tree used by the compatibility fixture."""
    result = subprocess.run(
        ['git', 'archive', '--format=tar', tag],
        cwd=INSTALLER.parent,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.decode(errors='replace'))
    destination.mkdir()
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode='r:') as archive:
        archive.extractall(destination, filter='data')
    return destination / 'install.py'


def wait_for_path(path: Path, process: subprocess.Popen, timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise AssertionError(f'process exited before synchronization point: {stdout}\n{stderr}')
        time.sleep(0.01)
    process.kill()
    stdout, stderr = process.communicate()
    raise AssertionError(f'timed out waiting for {path}: {stdout}\n{stderr}')


class InstallerTests(unittest.TestCase):
    def test_uninstall_v081_restores_user_state_and_removes_only_kit(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }
            architect = home / 'config/opencode/agents/architect.md'
            architect.parent.mkdir(parents=True)
            architect.write_text('user architect\n')
            cli = home / 'config/opencode/cli.json'
            cli.write_text(
                json.dumps(
                    {
                        'theme': 'user-theme',
                        'plugins': [
                            'foreign-plugin',
                            './plugins/agentic-kit',
                            {'package': 'another-plugin', 'options': {'enabled': True}},
                        ],
                    }
                )
                + '\n'
            )
            project_task = home / 'project/.agentic/tasks/kept/task.json'
            project_task.parent.mkdir(parents=True)
            project_task.write_text('{"kept": true}\n')
            foreign_plugin = home / 'config/opencode/plugins/foreign/index.ts'
            foreign_plugin.parent.mkdir(parents=True)
            foreign_plugin.write_text('export const foreign = true\n')

            installed = subprocess.run(
                [sys.executable, str(INSTALLER)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)

            result = subprocess.run(
                [sys.executable, str(INSTALLER), '--uninstall'],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('UNINSTALLED: 0.8.1', result.stdout)
            self.assertEqual(architect.read_text(), 'user architect\n')
            restored_cli = json.loads(cli.read_text())
            self.assertEqual(restored_cli['theme'], 'user-theme')
            self.assertEqual(
                restored_cli['plugins'],
                ['foreign-plugin', {'package': 'another-plugin', 'options': {'enabled': True}}],
            )
            self.assertEqual(foreign_plugin.read_text(), 'export const foreign = true\n')
            self.assertEqual(project_task.read_text(), '{"kept": true}\n')
            self.assertFalse((home / 'config/opencode/plugins/agentic-kit').exists())
            self.assertFalse((home / 'data/opencode-agentic/runner').exists())
            state = home / 'state/opencode-agentic/install'
            self.assertFalse((state / 'manifest.json').exists())
            self.assertFalse((state / 'backups').exists())

            repeated = subprocess.run(
                [sys.executable, str(INSTALLER), '--uninstall'],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertIn('NOT_INSTALLED', repeated.stdout)

    def test_uninstall_real_v080_fixture_restores_original_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }
            architect = home / 'config/opencode/agents/architect.md'
            architect.parent.mkdir(parents=True)
            architect.write_text('legacy user architect\n')
            cli = home / 'config/opencode/cli.json'
            cli.write_text('{"plugins": ["foreign-plugin"]}\n')
            tagged = write_tagged_release('v0.8.0', home / 'release-v0.8.0')
            installed = subprocess.run(
                [sys.executable, str(tagged)],
                cwd=INSTALLER.parent,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            legacy_state = home / 'state/opencode-agentic/preview-install'
            self.assertTrue((legacy_state / 'manifest.json').is_file())

            result = subprocess.run(
                [sys.executable, str(INSTALLER), '--uninstall'],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('UNINSTALLED: 0.8.0', result.stdout)
            self.assertEqual(architect.read_text(), 'legacy user architect\n')
            self.assertEqual(json.loads(cli.read_text())['plugins'], ['foreign-plugin'])
            self.assertFalse((home / 'config/opencode/plugins/agentic-preview').exists())
            self.assertFalse((home / 'data/opencode-agentic/preview/runner').exists())
            self.assertFalse((legacy_state / 'manifest.json').exists())
            self.assertFalse((legacy_state / 'backups').exists())

    def test_uninstall_refuses_modified_managed_file_without_partial_restore(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }
            installed = subprocess.run(
                [sys.executable, str(INSTALLER)], env=env, capture_output=True, check=False
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            changed = home / 'config/opencode/plugins/agentic-kit/index.ts'
            changed.write_text('user edit after installation\n')
            manifest = home / 'state/opencode-agentic/install/manifest.json'

            result = subprocess.run(
                [sys.executable, str(INSTALLER), '--uninstall'],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn('LOCAL_CHANGE', result.stderr)
            self.assertEqual(changed.read_text(), 'user edit after installation\n')
            self.assertTrue(manifest.exists())

    def test_uninstall_preserves_nonempty_backup_archives_and_reports_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }
            installed = subprocess.run(
                [sys.executable, str(INSTALLER)], env=env, capture_output=True, check=False
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            archive = home / 'state/opencode-agentic/install/backup-archives/backup-retained'
            archive.mkdir(parents=True)
            evidence = archive / 'unknown.bak'
            evidence.write_text('cannot prove disposable\n')

            result = subprocess.run(
                [sys.executable, str(INSTALLER), '--uninstall'],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(evidence.is_file())
            self.assertIn('RETAINED_BACKUPS', result.stdout)
            self.assertIn(str(archive.parent), result.stdout)

    def test_uninstall_refuses_ambiguous_dual_layout_before_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }
            installed = subprocess.run(
                [sys.executable, str(INSTALLER)], env=env, capture_output=True, check=False
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            current = home / 'state/opencode-agentic/install/manifest.json'
            before = current.read_bytes()
            managed = home / 'config/opencode/plugins/agentic-kit/index.ts'
            managed_before = managed.read_bytes()
            legacy = home / 'state/opencode-agentic/preview-install/manifest.json'
            legacy.parent.mkdir(parents=True)
            legacy.write_text('{"version": "1.2", "files": []}\n')

            result = subprocess.run(
                [sys.executable, str(INSTALLER), '--uninstall'],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn('MULTIPLE_INSTALLATIONS', result.stderr)
            self.assertEqual(current.read_bytes(), before)
            self.assertEqual(managed.read_bytes(), managed_before)

    def test_v081_layout_package_metadata_and_tui_registration(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'xdg-config'),
                'XDG_DATA_HOME': str(home / 'xdg-data'),
                'XDG_STATE_HOME': str(home / 'xdg-state'),
            }

            result = subprocess.run(
                [sys.executable, str(INSTALLER)],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('INSTALLED: 0.8.1', result.stdout)
            self.assertIn('python3 install.py --uninstall', result.stdout)
            self.assertNotIn('Run install.py --rollback', result.stdout)
            plugin = home / 'xdg-config/opencode/plugins/agentic-kit'
            self.assertEqual(
                json.loads((plugin / 'package.json').read_text())['name'],
                'opencode-agentic-kit',
            )
            self.assertTrue((home / 'xdg-data/opencode-agentic/runner/agentic_gate.py').is_file())
            manifest = home / 'xdg-state/opencode-agentic/install/manifest.json'
            self.assertTrue(manifest.is_file())
            self.assertEqual(json.loads(manifest.read_text())['product_version'], '0.8.1')
            cli = json.loads((home / 'xdg-config/opencode/cli.json').read_text())
            self.assertEqual(cli.get('plugins', []), [])
            discovered = list((home / 'xdg-config/opencode/plugins').glob('*/package.json'))
            self.assertEqual(discovered, [plugin / 'package.json'])
            package = json.loads(discovered[0].read_text())
            self.assertEqual(package['exports'], {'.': './index.ts', './tui': './tui.ts'})
            installed_tui = (plugin / 'tui.ts').read_text()
            commands = ('design', 'task', 'approve', 'implement', 'review', 'finalize', 'accept')
            for command in commands:
                self.assertIn(f'["{command}",', installed_tui)

            rolled_back = subprocess.run(
                [sys.executable, str(INSTALLER), '--rollback'],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
            self.assertFalse((home / 'xdg-config/opencode/cli.json').exists())
            self.assertFalse(any(plugin.glob('*')))
            self.assertFalse(any((home / 'xdg-data/opencode-agentic/runner').rglob('*.py')))
            self.assertFalse(any((home / 'xdg-config/opencode/agents').glob('*.md')))
            self.assertFalse(manifest.exists())

    def test_install_refuses_legacy_v080_manifest_without_creating_second_kit(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            legacy = home / 'state/opencode-agentic/preview-install/manifest.json'
            legacy.parent.mkdir(parents=True)
            legacy.write_text('{"version": "1.2", "files": []}\n')
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }

            for mode in ((), ('--dry-run',), ('--upgrade',)):
                result = subprocess.run(
                    [sys.executable, str(INSTALLER), *mode],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('LEGACY_INSTALLATION', result.stderr)
                self.assertIn('python3 install.py --uninstall', result.stderr)
                self.assertNotIn('v0.8.0 installer to rollback', result.stderr)
            self.assertFalse((home / 'config').exists())
            self.assertFalse((home / 'data').exists())
            self.assertFalse((home / 'state/opencode-agentic/install').exists())

    def test_runner_module_layout_is_fully_managed(self):
        mapping = INSTALLER_MODULE.targets()
        root = INSTALLER.parent
        managed_runner_sources = {
            source.relative_to(root).as_posix()
            for source in mapping.values()
            if source.is_relative_to(root / 'runner')
        }

        discovered_runner_sources = {
            path.relative_to(root).as_posix()
            for path in (root / 'runner').rglob('*.py')
            if '__pycache__' not in path.parts
        }
        self.assertEqual(
            managed_runner_sources,
            discovered_runner_sources,
            'Installer must manage every Python module in runner/',
        )

    def test_upgrade_adds_acceptance_modules_and_installed_runner_is_source_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = home / 'canonical'
            source.mkdir()
            shutil.copy2(INSTALLER, source / 'install.py')
            for relative in sorted(
                item.relative_to(INSTALLER.parent)
                for item in set(INSTALLER_MODULE.targets().values())
            ):
                destination = source / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(INSTALLER.parent / relative, destination)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }
            legacy_install = f"""\
import importlib.util
spec = importlib.util.spec_from_file_location('legacy_installer', {str(source / 'install.py')!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
targets = module.targets()
module.targets = lambda: {{dest: src for dest, src in targets.items()
                          if src.name not in {{'acceptance_checks.py', 'acceptance_criteria.py'}}}}
module.install()
"""
            installed = subprocess.run(
                [sys.executable, '-c', legacy_install],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            installed_root = home / 'data/opencode-agentic'
            acceptance = installed_root / 'runner/agentic/acceptance_checks.py'
            criteria = installed_root / 'runner/agentic/acceptance_criteria.py'
            self.assertFalse(acceptance.exists())
            self.assertFalse(criteria.exists())

            upgraded = subprocess.run(
                [sys.executable, str(source / 'install.py'), '--upgrade'],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            self.assertIn('python3 install.py --uninstall', upgraded.stdout)
            self.assertEqual(
                acceptance.read_bytes(),
                (source / 'runner/agentic/acceptance_checks.py').read_bytes(),
            )
            self.assertEqual(
                criteria.read_bytes(),
                (source / 'runner/agentic/acceptance_criteria.py').read_bytes(),
            )

            shutil.rmtree(source)
            unrelated = home / 'unrelated'
            unrelated.mkdir()
            runner = installed_root / 'runner/agentic_gate.py'
            result = subprocess.run(
                [sys.executable, str(runner), '--help'],
                cwd=unrelated,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('check', result.stdout)

    def test_dry_run_default_install_alias_idempotent_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }
            arch = home / 'config/opencode/agents/architect.md'
            arch.parent.mkdir(parents=True)
            arch.write_text('my custom role\n')
            cli = home / 'config/opencode/cli.json'
            cli.write_text(json.dumps({'animations': False}) + '\n')
            original_cli = cli.read_bytes()

            def run(*args):
                return subprocess.run(
                    [sys.executable, str(INSTALLER), *args],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            dry = run('--dry-run')
            self.assertEqual(dry.returncode, 0, dry.stderr)
            self.assertIn('BACKUP+REPLACE', dry.stdout)
            self.assertEqual(arch.read_text(), 'my custom role\n')
            self.assertEqual(cli.read_bytes(), original_cli)
            self.assertFalse((home / 'state/opencode-agentic/install').exists())
            install = run()
            self.assertEqual(install.returncode, 0, install.stderr)
            self.assertIn('INSTALLED: 0.8.1', install.stdout)
            manifest = home / 'state/opencode-agentic/install/manifest.json'
            self.assertEqual(json.loads(manifest.read_text())['version'], '1.2')
            self.assertIn('.agentic/drafts/spec.md', arch.read_text())
            self.assertFalse(json.loads(cli.read_text())['animations'])
            self.assertNotIn(
                './plugins/agentic-kit', json.loads(cli.read_text()).get('plugins', [])
            )
            same = run('--install')
            self.assertIn('ALREADY_INSTALLED', same.stdout)
            rollback = run('--rollback')
            self.assertEqual(rollback.returncode, 0, rollback.stderr)
            self.assertEqual(arch.read_text(), 'my custom role\n')
            self.assertEqual(cli.read_bytes(), original_cli)
            plugin = home / 'config/opencode/plugins/agentic-kit/index.ts'
            self.assertFalse(plugin.exists())

    def test_cli_modes_are_mutually_exclusive_and_help_is_current(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }

            def run(*args):
                return subprocess.run(
                    [sys.executable, str(INSTALLER), *args],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            help_result = run('--help')
            self.assertEqual(help_result.returncode, 0, help_result.stderr)
            normalized_help = ' '.join(help_result.stdout.lower().split())
            self.assertIn('--dry-run', help_result.stdout)
            self.assertIn('default', normalized_help)
            self.assertIn('compatibility alias', normalized_help)
            self.assertIn('existing managed installation', normalized_help)
            self.assertIn('restore', normalized_help)
            self.assertIn('--uninstall', help_result.stdout)
            self.assertIn('v0.8.0 or v0.8.1', help_result.stdout)
            self.assertIn('recommended full removal', normalized_help)
            self.assertIn('retaining backups', normalized_help)

            conflict = run('--dry-run', '--install')
            self.assertNotEqual(conflict.returncode, 0)
            self.assertIn('not allowed with argument', conflict.stderr)
            self.assertFalse((home / 'config').exists())
            self.assertFalse((home / 'data').exists())
            self.assertFalse((home / 'state').exists())

    def test_upgrade_requires_existing_installation_and_only_creates_stable_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }

            result = subprocess.run(
                [sys.executable, str(INSTALLER), '--upgrade'],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn('NOT_INSTALLED', result.stderr)
            self.assertFalse((home / 'config').exists())
            self.assertFalse((home / 'data').exists())
            state = home / 'state/opencode-agentic/install'
            self.assertEqual([path.name for path in state.iterdir()], ['installer.lock'])
            self.assertFalse((state / 'manifest.json').exists())
            self.assertFalse((state / 'backups').exists())

    def test_upgrade_from_canonical_sources_preserves_original_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            source = home / 'canonical'
            source.mkdir()
            shutil.copy2(INSTALLER, source / 'install.py')
            root = INSTALLER.parent
            for rel in sorted(
                item.relative_to(INSTALLER.parent)
                for item in set(INSTALLER_MODULE.targets().values())
            ):
                dest = source / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / rel, dest)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }
            arch = home / 'config/opencode/agents/architect.md'
            arch.parent.mkdir(parents=True)
            arch.write_text('original custom architect\n')

            def run(*args):
                return subprocess.run(
                    [sys.executable, str(source / 'install.py'), *args],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            self.assertEqual(run('--install').returncode, 0)
            plugin = home / 'config/opencode/plugins/agentic-kit/index.ts'
            initial = plugin.read_text()
            manifest = home / 'state/opencode-agentic/install/manifest.json'
            old = json.loads(manifest.read_text())
            old['version'] = '1.2-preview.1'
            old.pop('tui_config')
            cli = home / 'config/opencode/cli.json'
            cli.write_text(json.dumps({'animations': True}) + '\n')
            tui = home / 'config/opencode/plugins/agentic-kit/tui.ts'
            package = home / 'config/opencode/plugins/agentic-kit/package.json'
            package_data = json.loads(package.read_text())
            self.assertEqual(package_data['exports']['./tui'], './tui.ts')
            old['files'] = [entry for entry in old['files'] if entry['dest'] != str(tui)]
            tui.unlink()
            for entry in old['files']:
                entry['source'] = entry['source'].replace('canonical', 'legacy-preview-source')
            manifest.write_text(json.dumps(old))
            (source / 'plugin/index.ts').write_text(initial + '\n// v1.2-preview.2 change\n')
            # A source change must never be silently considered installed.
            refused = run()
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn('UPGRADE_REQUIRED', refused.stderr)
            self.assertEqual(plugin.read_text(), initial)
            upgraded = run('--upgrade')
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            self.assertIn('UPGRADED: 0.8.1', upgraded.stdout)
            self.assertEqual(json.loads(manifest.read_text())['version'], '1.2')
            self.assertIn('v1.2-preview.2 change', plugin.read_text())
            self.assertEqual(tui.read_text(), (source / 'plugin/tui.ts').read_text())
            self.assertEqual(json.loads(package.read_text())['version'], '0.8.1')
            self.assertNotIn(
                './plugins/agentic-kit', json.loads(cli.read_text()).get('plugins', [])
            )
            self.assertIn('ALREADY_INSTALLED', run('--install').stdout)
            self.assertEqual(run('--rollback').returncode, 0)
            self.assertEqual(arch.read_text(), 'original custom architect\n')
            self.assertFalse(plugin.exists())
            self.assertFalse(tui.exists())

    def test_cli_plugin_objects_preserved_and_restored(self):
        """Valid CLI package directives with options must survive install/rollback."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }
            cli = home / 'config/opencode/cli.json'
            cli.parent.mkdir(parents=True)
            plugin_entry = {'package': 'my-cli-plugin', 'options': {'compact': True}}
            cli.write_text(
                json.dumps(
                    {
                        'animations': False,
                        'plugins': [
                            plugin_entry,
                            './plugins/other',
                            './plugins/agentic-preview/tui.ts',
                            str(cli.parent / 'plugins/agentic-preview/tui.ts'),
                            './plugins/agentic-kit',
                            './plugins/agentic-kit',
                            {
                                'package': './plugins/agentic-kit',
                                'options': {'duplicate': True},
                            },
                            ['./plugins/agentic-preview/tui.ts', {'legacy': True}],
                        ],
                    }
                )
                + '\n'
            )

            def run(*args):
                return subprocess.run(
                    [sys.executable, str(INSTALLER), *args],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            installed = run('--install')
            self.assertEqual(installed.returncode, 0, installed.stderr)
            configured = json.loads(cli.read_text())
            self.assertEqual(configured['plugins'], [plugin_entry, './plugins/other'])
            self.assertEqual(configured['plugins'].count('./plugins/agentic-kit'), 0)
            self.assertNotIn('./plugins/agentic-preview/tui.ts', configured['plugins'])
            self.assertNotIn(
                str(cli.parent / 'plugins/agentic-preview/tui.ts'), configured['plugins']
            )
            self.assertFalse(configured['animations'])
            self.assertIn('ALREADY_INSTALLED', run('--install').stdout)

            rolled_back = run('--rollback')
            self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
            restored = json.loads(cli.read_text())
            self.assertEqual(restored['plugins'][:2], [plugin_entry, './plugins/other'])
            self.assertNotIn('./plugins/agentic-kit', restored['plugins'])
            self.assertNotIn('./plugins/agentic-preview/tui.ts', restored['plugins'])
            self.assertNotIn(
                str(cli.parent / 'plugins/agentic-preview/tui.ts'), restored['plugins']
            )
            self.assertFalse(restored['animations'])

    def test_upgrade_legacy_without_cli_config_creates_and_rolls_back(self):
        """A v1.2-preview.1 installation may have no cli.json or TUI target."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }

            def run(*args):
                return subprocess.run(
                    [sys.executable, str(INSTALLER), *args],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            self.assertEqual(run('--install').returncode, 0)
            manifest = home / 'state/opencode-agentic/install/manifest.json'
            old = json.loads(manifest.read_text())
            old['version'] = '1.2-preview.1'
            old.pop('tui_config')
            cli = home / 'config/opencode/cli.json'
            cli.unlink()
            plugin_dir = home / 'config/opencode/plugins/agentic-kit'
            for entry in list(old['files']):
                if Path(entry['dest']).name in {'tui.ts', 'package.json'}:
                    Path(entry['dest']).unlink()
            old['files'] = [
                entry
                for entry in old['files']
                if Path(entry['dest']).name not in {'tui.ts', 'package.json'}
            ]
            manifest.write_text(json.dumps(old))

            upgraded = run('--upgrade')
            self.assertEqual(upgraded.returncode, 0, upgraded.stderr)
            self.assertTrue(cli.is_file())
            self.assertNotIn(
                './plugins/agentic-kit', json.loads(cli.read_text()).get('plugins', [])
            )
            self.assertTrue((plugin_dir / 'tui.ts').is_file())
            self.assertIn('ALREADY_INSTALLED', run('--install').stdout)

            rollback = run('--rollback')
            self.assertEqual(rollback.returncode, 0, rollback.stderr)
            self.assertFalse(cli.exists(), 'rollback must restore the absence of cli.json')
            self.assertFalse((plugin_dir / 'tui.ts').exists())
            self.assertFalse((plugin_dir / 'package.json').exists())
            self.assertFalse(manifest.exists())

    def test_upgrade_refuses_locally_modified_targets(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }

            def run(*args):
                return subprocess.run(
                    [sys.executable, str(INSTALLER), *args],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            self.assertEqual(run('--install').returncode, 0)
            plugin = home / 'config/opencode/plugins/agentic-kit/index.ts'
            plugin.write_text('// local changes\n')
            result = run('--upgrade')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('LOCAL_CHANGE', result.stderr)
            self.assertEqual(plugin.read_text(), '// local changes\n')

    def test_modified_installed_files_not_overwritten_on_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }

            def run(*args):
                return subprocess.run(
                    [sys.executable, str(INSTALLER), *args],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            self.assertEqual(run('--install').returncode, 0)
            plugin = home / 'config/opencode/plugins/agentic-kit/index.ts'
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
            for rel in sorted(
                item.relative_to(INSTALLER.parent)
                for item in set(INSTALLER_MODULE.targets().values())
            ):
                dest = source / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / rel, dest)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }
            install = subprocess.run(
                [sys.executable, str(source / 'install.py'), '--install'],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(install.returncode, 0, install.stderr)
            shutil.rmtree(source)

            project = home / 'project'
            project.mkdir()
            subprocess.run(['git', 'init', '-q', str(project)], check=True)
            runner = home / 'data/opencode-agentic/runner/agentic_gate.py'
            result = subprocess.run(
                [sys.executable, str(runner), '--project', str(project), 'fingerprint'],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(result.stdout.strip(), r'^[0-9a-f]{64}$')

            rollback = subprocess.run(
                [sys.executable, str(INSTALLER), '--rollback'],
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(rollback.returncode, 0, rollback.stderr)
            self.assertFalse((runner.parent / 'agentic/__init__.py').exists())
            self.assertFalse((runner.parent / 'agentic/cli.py').exists())
            self.assertFalse((runner.parent / 'agentic/commandline.py').exists())

    def test_legacy_manifest_without_any_backups_rolls_back_created_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }

            def run(*args):
                return subprocess.run(
                    [sys.executable, str(INSTALLER), *args],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            installed = run('--install')
            self.assertEqual(installed.returncode, 0, installed.stderr)
            manifest = home / 'state/opencode-agentic/install/manifest.json'
            data = json.loads(manifest.read_text())
            for entry in [*data['files'], data['tui_config']]:
                self.assertFalse(entry['existed'])
                self.assertIsNone(entry['backup'])
                entry.pop('backup_hash', None)
            manifest.write_text(json.dumps(data))

            rolled_back = run('--rollback')
            self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
            self.assertFalse(manifest.exists())

    def test_atomic_write_removes_temporary_file_when_fsync_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            destination = directory / 'target'
            with (
                mock.patch.object(INSTALLER_MODULE.os, 'fsync', side_effect=OSError('disk error')),
                self.assertRaisesRegex(OSError, 'disk error'),
            ):
                INSTALLER_MODULE.atomic_write(destination, b'new content')
            self.assertFalse(destination.exists())
            self.assertEqual(list(directory.iterdir()), [])


class InstallTransactionCrashTests(unittest.TestCase):
    CRASH_EXIT = 91

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        self.env = {
            **os.environ,
            'HOME': str(self.home),
            'XDG_CONFIG_HOME': str(self.home / 'config'),
            'XDG_DATA_HOME': str(self.home / 'data'),
            'XDG_STATE_HOME': str(self.home / 'state'),
        }
        self.arch = self.home / 'config/opencode/agents/architect.md'
        self.arch.parent.mkdir(parents=True)
        self.arch.write_bytes(b'original architect\x00\xff\n')
        self.cli = self.home / 'config/opencode/cli.json'
        self.cli.write_bytes(b'{"animations": false}\n')
        self.original_arch = self.arch.read_bytes()
        self.original_cli = self.cli.read_bytes()
        self.state = self.home / 'state/opencode-agentic/install'
        self.manifest = self.state / 'manifest.json'
        self.transaction = self.state / 'transaction.json'

    def tearDown(self):
        self.tempdir.cleanup()

    def run_cli(self, *args, installer=INSTALLER):
        return subprocess.run(
            [sys.executable, str(installer), *args],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def crash_install(self, stage, *, occurrence=1, upgrade=False, installer=INSTALLER):
        code = f"""\
import importlib.util
import os
spec = importlib.util.spec_from_file_location("crash_installer", {str(installer)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
seen = 0
def checkpoint(current, dest=None):
    global seen
    if current == {stage!r}:
        seen += 1
        if seen == {occurrence!r}:
            os._exit({self.CRASH_EXIT})
module.install_checkpoint = checkpoint
module.install(upgrade={upgrade!r})
"""
        return subprocess.run(
            [sys.executable, '-c', code], env=self.env, text=True, capture_output=True, check=False
        )

    def assert_interrupted_install_recovers(self, stage, occurrence=1):
        result = self.crash_install(stage, occurrence=occurrence)
        self.assertEqual(result.returncode, self.CRASH_EXIT, result.stderr)
        self.assertTrue(self.transaction.exists())

        repeated = self.run_cli('--install')
        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn('INCOMPLETE_TRANSACTION', repeated.stderr)

        recovered = self.run_cli('--rollback')
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(self.arch.read_bytes(), self.original_arch)
        self.assertEqual(self.cli.read_bytes(), self.original_cli)
        self.assertFalse(self.manifest.exists())
        self.assertFalse(self.transaction.exists())
        self.assertFalse(
            self.home.joinpath('config/opencode/plugins/agentic-kit/index.ts').exists()
        )

    def install_previous_release_missing_one_target(self):
        reviewer = self.home / 'config/opencode/agents/reviewer.md'
        reviewer.parent.mkdir(parents=True, exist_ok=True)
        reviewer.write_bytes(b'original reviewer\n')
        code = f"""\
import importlib.util
spec = importlib.util.spec_from_file_location("previous_installer", {str(INSTALLER)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
all_targets = module.targets()
reviewer = module.directories()[0] / "agents/reviewer.md"
module.targets = lambda: {{dest: source for dest, source in all_targets.items()
                          if dest != reviewer}}
module.install()
"""
        installed = subprocess.run(
            [sys.executable, '-c', code], env=self.env, text=True, capture_output=True, check=False
        )
        self.assertEqual(installed.returncode, 0, installed.stderr)
        return reviewer

    def assert_prejournal_upgrade_crash_is_retryable(self, stage, occurrence=1):
        reviewer = self.install_previous_release_missing_one_target()
        before_manifest = self.manifest.read_bytes()
        before_targets = {
            Path(entry['dest']): Path(entry['dest']).read_bytes()
            for entry in json.loads(before_manifest)['files']
        }
        before_targets[reviewer] = reviewer.read_bytes()
        original_backups = {
            path.relative_to(self.state / 'backups'): path.read_bytes()
            for path in (self.state / 'backups').rglob('*')
            if path.is_file()
        }

        crashed = self.crash_install(stage, occurrence=occurrence, upgrade=True)
        self.assertEqual(crashed.returncode, self.CRASH_EXIT, crashed.stderr)
        self.assertFalse(self.transaction.exists())
        self.assertEqual(self.manifest.read_bytes(), before_manifest)
        for target, content in before_targets.items():
            self.assertEqual(target.read_bytes(), content)
        for relative, content in original_backups.items():
            self.assertEqual((self.state / 'backups' / relative).read_bytes(), content)

        repeated = self.run_cli('--upgrade')
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        upgraded = json.loads(self.manifest.read_text())
        reviewer_entry = next(
            entry for entry in upgraded['files'] if entry['dest'] == str(reviewer)
        )
        self.assertEqual(Path(reviewer_entry['backup']).read_bytes(), b'original reviewer\n')

        rolled_back = self.run_cli('--rollback')
        self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
        self.assertEqual(reviewer.read_bytes(), b'original reviewer\n')
        self.assertEqual(self.arch.read_bytes(), self.original_arch)
        self.assertEqual(self.cli.read_bytes(), self.original_cli)

    def test_upgrade_crash_before_first_new_backup_is_retryable(self):
        self.assert_prejournal_upgrade_crash_is_retryable('before_upgrade_backups')

    def test_upgrade_crash_after_one_new_backup_is_retryable(self):
        self.assert_prejournal_upgrade_crash_is_retryable('after_upgrade_backup')

    def test_upgrade_crash_after_all_new_backups_is_retryable(self):
        self.assert_prejournal_upgrade_crash_is_retryable('after_upgrade_backups')

    def test_upgrade_crash_during_recovery_generation_is_retryable(self):
        self.assert_prejournal_upgrade_crash_is_retryable('after_upgrade_recovery_backup')

    def test_hard_exit_before_first_target_change_is_recoverable(self):
        self.assert_interrupted_install_recovers('after_transaction_prepare')

    def test_hard_exit_after_tui_config_change_is_recoverable(self):
        self.assert_interrupted_install_recovers('after_target_change', 1)

    def test_hard_exit_after_first_release_target_is_recoverable(self):
        self.assert_interrupted_install_recovers('after_target_change', 2)

    def test_hard_exit_after_multiple_release_targets_is_recoverable(self):
        self.assert_interrupted_install_recovers('after_target_change', 4)

    def test_hard_exit_after_all_targets_before_manifest_is_recoverable(self):
        self.assert_interrupted_install_recovers('before_manifest_commit')

    def test_hard_exit_after_manifest_before_transaction_commit_is_recoverable(self):
        self.assert_interrupted_install_recovers('after_manifest_commit')

    def test_hard_exit_during_transaction_recovery_is_resumable(self):
        result = self.crash_install('after_target_change', occurrence=4)
        self.assertEqual(result.returncode, self.CRASH_EXIT, result.stderr)
        code = f"""\
import importlib.util
import os
spec = importlib.util.spec_from_file_location("recovery_installer", {str(INSTALLER)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
def checkpoint(stage, dest=None):
    if stage == "after_recovery_target":
        os._exit({self.CRASH_EXIT})
module.install_checkpoint = checkpoint
module.rollback()
"""
        interrupted = subprocess.run(
            [sys.executable, '-c', code], env=self.env, text=True, capture_output=True, check=False
        )
        self.assertEqual(interrupted.returncode, self.CRASH_EXIT, interrupted.stderr)
        self.assertTrue(self.transaction.exists())

        resumed = self.run_cli('--rollback')
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(self.arch.read_bytes(), self.original_arch)
        self.assertEqual(self.cli.read_bytes(), self.original_cli)
        self.assertFalse(self.transaction.exists())

    def test_local_change_after_install_crash_is_never_overwritten(self):
        result = self.crash_install('after_target_change', occurrence=2)
        self.assertEqual(result.returncode, self.CRASH_EXIT, result.stderr)
        self.arch.write_bytes(b'user edit after crash\n')

        recovered = self.run_cli('--rollback')

        self.assertNotEqual(recovered.returncode, 0)
        self.assertIn('LOCAL_CHANGE', recovered.stderr)
        self.assertEqual(self.arch.read_bytes(), b'user edit after crash\n')
        self.assertTrue(self.transaction.exists())

    def test_recovery_backup_hash_is_checked_before_any_restore(self):
        result = self.crash_install('after_target_change', occurrence=2)
        self.assertEqual(result.returncode, self.CRASH_EXIT, result.stderr)
        data = json.loads(self.transaction.read_text())
        entry = next(item for item in data['targets'] if item['dest'] == str(self.arch))
        backup = Path(entry['backup'])
        self.assertEqual(entry['backup_hash'], INSTALLER_MODULE.digest(self.original_arch))
        backup.write_bytes(b'corrupt')
        changed_cli = self.cli.read_bytes()

        recovered = self.run_cli('--rollback')

        self.assertNotEqual(recovered.returncode, 0)
        self.assertIn('BACKUP_CHANGED', recovered.stderr)
        self.assertEqual(self.cli.read_bytes(), changed_cli)
        self.assertTrue(self.transaction.exists())

    def test_fsync_failure_after_cli_replace_leaves_recoverable_transaction(self):
        self.cli.write_text(
            json.dumps({'animations': False, 'plugins': ['./plugins/agentic-kit']}) + '\n'
        )
        self.original_cli = self.cli.read_bytes()
        code = f"""\
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location("fsync_installer", {str(INSTALLER)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
original = module.atomic_write
def fail_after_replace(dest, content, **kwargs):
    if dest != module.directories()[0] / "cli.json":
        return original(dest, content, **kwargs)
    calls = 0
    real_fsync = module.os.fsync
    def fsync(fd):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync failed after replace")
        return real_fsync(fd)
    module.os.fsync = fsync
    try:
        return original(dest, content, **kwargs)
    finally:
        module.os.fsync = real_fsync
module.atomic_write = fail_after_replace
module.install()
"""
        failed = subprocess.run(
            [sys.executable, '-c', code], env=self.env, text=True, capture_output=True, check=False
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertNotEqual(self.cli.read_bytes(), self.original_cli)
        self.assertTrue(self.transaction.exists())

        recovered = self.run_cli('--rollback')
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        restored = json.loads(self.cli.read_text())
        self.assertFalse(restored['animations'])
        self.assertNotIn('./plugins/agentic-kit', restored['plugins'])
        self.assertEqual(self.arch.read_bytes(), self.original_arch)

    def test_interrupted_upgrade_recovers_previous_release_then_full_rollback(self):
        source = self.home / 'canonical'
        source.mkdir()
        shutil.copy2(INSTALLER, source / 'install.py')
        for rel in sorted(
            item.relative_to(INSTALLER.parent) for item in set(INSTALLER_MODULE.targets().values())
        ):
            destination = source / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(INSTALLER.parent / rel, destination)
        installed = self.run_cli('--install', installer=source / 'install.py')
        self.assertEqual(installed.returncode, 0, installed.stderr)
        before_manifest = self.manifest.read_bytes()
        before_targets = {
            Path(entry['dest']): Path(entry['dest']).read_bytes()
            for entry in json.loads(before_manifest)['files']
        }
        for relative in ('plugin/index.ts', 'plugin/tui.ts', 'agents/architect.md'):
            changed = source / relative
            changed.write_bytes(changed.read_bytes() + b'\n// upgraded\n')

        crashed = self.crash_install(
            'after_target_change', occurrence=3, upgrade=True, installer=source / 'install.py'
        )
        self.assertEqual(crashed.returncode, self.CRASH_EXIT, crashed.stderr)
        repeated = self.run_cli('--upgrade', installer=source / 'install.py')
        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn('INCOMPLETE_TRANSACTION', repeated.stderr)

        recovered = self.run_cli('--rollback', installer=source / 'install.py')
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(self.manifest.read_bytes(), before_manifest)
        for target, content in before_targets.items():
            self.assertEqual(target.read_bytes(), content)

        full = self.run_cli('--rollback', installer=source / 'install.py')
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertEqual(self.arch.read_bytes(), self.original_arch)
        self.assertEqual(self.cli.read_bytes(), self.original_cli)

    def test_hard_exit_during_upgrade_recovery_is_resumable(self):
        self.install_previous_release_missing_one_target()
        crashed = self.crash_install('after_target_change', occurrence=1, upgrade=True)
        self.assertEqual(crashed.returncode, self.CRASH_EXIT, crashed.stderr)
        self.assertTrue(self.transaction.exists())
        code = f"""\
import importlib.util
import os
spec = importlib.util.spec_from_file_location("upgrade_recovery", {str(INSTALLER)!r})
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
def checkpoint(stage, dest=None):
    if stage == "after_recovery_target":
        os._exit({self.CRASH_EXIT})
module.install_checkpoint = checkpoint
module.rollback()
"""
        interrupted = subprocess.run(
            [sys.executable, '-c', code], env=self.env, text=True, capture_output=True, check=False
        )
        self.assertEqual(interrupted.returncode, self.CRASH_EXIT, interrupted.stderr)
        self.assertTrue(self.transaction.exists())

        resumed = self.run_cli('--rollback')
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertFalse(self.transaction.exists())
        repeated = self.run_cli('--upgrade')
        self.assertEqual(repeated.returncode, 0, repeated.stderr)


class InstallerFilesystemTests(unittest.TestCase):
    def test_install_rollback_install_rollback_archives_retained_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }

            def run(*args):
                return subprocess.run(
                    [sys.executable, str(INSTALLER), *args],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            arch = home / 'config/opencode/agents/architect.md'
            arch.parent.mkdir(parents=True)
            arch.write_bytes(b'first original\n')
            self.assertEqual(run('--install').returncode, 0)
            manifest = home / 'state/opencode-agentic/install/manifest.json'
            backup = Path(
                next(
                    entry['backup']
                    for entry in json.loads(manifest.read_text())['files']
                    if entry['dest'] == str(arch)
                )
            )
            retained = backup.read_bytes()
            self.assertEqual(run('--rollback').returncode, 0)
            arch.write_bytes(b'second original\n')

            repeated = run('--install')

            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            archives = list((manifest.parent / 'backup-archives').iterdir())
            self.assertEqual(len(archives), 1)
            self.assertEqual((archives[0] / backup.name).read_bytes(), retained)
            current = json.loads(manifest.read_text())
            current_backup = Path(
                next(entry['backup'] for entry in current['files'] if entry['dest'] == str(arch))
            )
            self.assertEqual(current_backup.read_bytes(), b'second original\n')
            self.assertEqual(current_backup.parent, manifest.parent / 'backups')
            self.assertEqual(run('--rollback').returncode, 0)
            self.assertEqual(arch.read_bytes(), b'second original\n')
            self.assertEqual((archives[0] / backup.name).read_bytes(), retained)

    def test_two_reinstall_cycles_preserve_each_backup_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }
            arch = home / 'config/opencode/agents/architect.md'
            arch.parent.mkdir(parents=True)
            manifest = home / 'state/opencode-agentic/install/manifest.json'

            def run(*args):
                return subprocess.run(
                    [sys.executable, str(INSTALLER), *args],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            originals = [b'first\n', b'second\n', b'third\n']
            for original in originals:
                arch.write_bytes(original)
                installed = run('--install')
                self.assertEqual(installed.returncode, 0, installed.stderr)
                data = json.loads(manifest.read_text())
                entry = next(item for item in data['files'] if item['dest'] == str(arch))
                self.assertEqual(entry['backup_hash'], INSTALLER_MODULE.digest(original))
                rolled_back = run('--rollback')
                self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
                self.assertEqual(arch.read_bytes(), original)

            archives = sorted((manifest.parent / 'backup-archives').iterdir())
            self.assertEqual(len(archives), 2)
            self.assertEqual(
                {
                    next(path for path in archive.iterdir() if path.name == '3.bak').read_bytes()
                    for archive in archives
                },
                {b'first\n', b'second\n'},
            )

    def test_fifo_managed_target_fails_promptly_instead_of_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }
            arch = home / 'config/opencode/agents/architect.md'
            arch.parent.mkdir(parents=True)
            os.mkfifo(arch)

            try:
                result = subprocess.run(
                    [sys.executable, str(INSTALLER), '--install'],
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=2,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                self.fail(f'installer blocked while opening FIFO: {exc}')

            self.assertNotEqual(result.returncode, 0)
            self.assertIn('UNSAFE_PATH', result.stderr)
            self.assertTrue(stat.S_ISFIFO(arch.stat().st_mode))
            self.assertFalse((home / 'state/opencode-agentic/install/manifest.json').exists())

    def test_regular_managed_file_read_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'regular'
            target.write_bytes(b'regular content\n')
            self.assertEqual(INSTALLER_MODULE.managed_read(target), b'regular content\n')

    def test_active_manifest_never_archives_current_backups(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env = {
                **os.environ,
                'HOME': tmp,
                'XDG_CONFIG_HOME': str(home / 'config'),
                'XDG_DATA_HOME': str(home / 'data'),
                'XDG_STATE_HOME': str(home / 'state'),
            }
            arch = home / 'config/opencode/agents/architect.md'
            arch.parent.mkdir(parents=True)
            arch.write_bytes(b'original\n')

            def run(*args):
                return subprocess.run(
                    [sys.executable, str(INSTALLER), *args],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )

            self.assertEqual(run('--install').returncode, 0)
            state = home / 'state/opencode-agentic/install'
            snapshot = {path.name: path.read_bytes() for path in (state / 'backups').iterdir()}
            same = run('--install')
            self.assertEqual(same.returncode, 0, same.stderr)
            self.assertIn('ALREADY_INSTALLED', same.stdout)
            self.assertEqual(
                {path.name: path.read_bytes() for path in (state / 'backups').iterdir()},
                snapshot,
            )
            self.assertFalse((state / 'backup-archives').exists())

    def test_existing_backup_is_never_replaced_without_archiving(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup = Path(tmp) / '0.bak'
            backup.write_bytes(b'retained\n')
            with self.assertRaisesRegex(RuntimeError, 'BACKUP_EXISTS'):
                INSTALLER_MODULE.write_new_backups([(backup, b'replacement\n')])
            self.assertEqual(backup.read_bytes(), b'retained\n')

    def test_upgrade_generation_collision_never_adopts_foreign_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / 'state'
            backups = state / 'backups'
            foreign = backups / 'upgrade-0000000000000000'
            foreign.mkdir(parents=True)
            marker = foreign / 'foreign.bak'
            marker.write_bytes(b'foreign\n')
            transaction = INSTALLER_MODULE.InstallTransaction(state)
            tokens = iter(('0000000000000000', '1111111111111111'))

            with mock.patch.object(
                INSTALLER_MODULE.secrets, 'token_hex', side_effect=lambda _: next(tokens)
            ):
                generation = transaction.create_backup_generation(backups, 'upgrade')

            self.assertEqual(generation, backups / 'upgrade-1111111111111111')
            self.assertTrue(generation.is_dir())
            self.assertEqual(marker.read_bytes(), b'foreign\n')

    def test_rename_noreplace_distinguishes_collision_from_other_errors(self):
        renameat2 = mock.Mock(return_value=-1)
        library = mock.Mock(renameat2=renameat2)
        with mock.patch.object(INSTALLER_MODULE.ctypes, 'CDLL', return_value=library):
            with (
                mock.patch.object(
                    INSTALLER_MODULE.ctypes, 'get_errno', return_value=INSTALLER_MODULE.errno.EEXIST
                ),
                self.assertRaises(FileExistsError),
            ):
                INSTALLER_MODULE._rename_noreplace(
                    'backups',
                    'archive',
                    source_dir=3,
                    destination_dir=4,
                )
            with (
                mock.patch.object(
                    INSTALLER_MODULE.ctypes, 'get_errno', return_value=INSTALLER_MODULE.errno.EIO
                ),
                self.assertRaises(OSError) as raised,
            ):
                INSTALLER_MODULE._rename_noreplace(
                    'backups',
                    'archive',
                    source_dir=3,
                    destination_dir=4,
                )
        self.assertEqual(raised.exception.errno, INSTALLER_MODULE.errno.EIO)


class BackupArchiveBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        self.env = mock.patch.dict(
            os.environ,
            {
                'HOME': str(self.home),
                'XDG_CONFIG_HOME': str(self.home / 'config'),
                'XDG_DATA_HOME': str(self.home / 'data'),
                'XDG_STATE_HOME': str(self.home / 'state'),
            },
        )
        self.env.start()
        self.arch = self.home / 'config/opencode/agents/architect.md'
        self.arch.parent.mkdir(parents=True)
        self.arch.write_bytes(b'first original\n')
        with contextlib.redirect_stdout(io.StringIO()):
            INSTALLER_MODULE.install()
            INSTALLER_MODULE.rollback()
        self.state = self.home / 'state/opencode-agentic/install'
        self.retained = {
            path.name: path.read_bytes() for path in (self.state / 'backups').iterdir()
        }
        self.arch.write_bytes(b'second original\n')

    def tearDown(self):
        self.env.stop()
        self.tempdir.cleanup()

    def interrupt_install_at(self, expected_stage, message):
        def interrupt(stage, archive=None):
            if stage == expected_stage:
                raise RuntimeError(message)

        with (
            mock.patch.object(INSTALLER_MODULE, 'archive_checkpoint', interrupt),
            self.assertRaisesRegex(RuntimeError, message),
        ):
            INSTALLER_MODULE.install()

    def assert_retained_archive(self):
        archives = list((self.state / 'backup-archives').iterdir())
        self.assertEqual(len(archives), 1)
        self.assertEqual(
            {path.name: path.read_bytes() for path in archives[0].iterdir()},
            self.retained,
        )
        return archives[0]

    def test_archive_name_collision_preserves_existing_archive(self):
        archive_root = self.state / 'backup-archives'
        existing = archive_root / 'backup-collision'
        existing.mkdir(parents=True)
        marker = existing / 'marker'
        marker.write_bytes(b'never replace\n')

        archive_names = iter(['collision', 'unique'])

        def controlled_token(size):
            return next(archive_names, f'temp-{time.monotonic_ns()}')

        with (
            mock.patch.object(INSTALLER_MODULE.secrets, 'token_hex', side_effect=controlled_token),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            INSTALLER_MODULE.install()

        self.assertEqual(marker.read_bytes(), b'never replace\n')
        archived = archive_root / 'backup-unique'
        self.assertEqual(
            {path.name: path.read_bytes() for path in archived.iterdir()},
            self.retained,
        )

    def test_archive_failure_before_move_keeps_old_backups_and_targets(self):
        with (
            mock.patch.object(
                INSTALLER_MODULE,
                '_rename_noreplace',
                side_effect=OSError('simulated rename failure'),
            ),
            self.assertRaisesRegex(OSError, 'simulated rename failure'),
        ):
            INSTALLER_MODULE.install()

        self.assertEqual(
            {path.name: path.read_bytes() for path in (self.state / 'backups').iterdir()},
            self.retained,
        )
        self.assertEqual(self.arch.read_bytes(), b'second original\n')
        self.assertFalse((self.state / 'manifest.json').exists())

    def test_interruption_after_archive_move_is_resumable_without_backup_loss(self):
        self.interrupt_install_at('after_backup_archive', 'simulated archive interruption')
        archive = self.assert_retained_archive()
        self.assertFalse((self.state / 'manifest.json').exists())
        self.assertFalse((self.state / 'backups').exists())
        self.assertEqual(self.arch.read_bytes(), b'second original\n')

        with contextlib.redirect_stdout(io.StringIO()):
            INSTALLER_MODULE.install()
            INSTALLER_MODULE.rollback()
        self.assertEqual(self.arch.read_bytes(), b'second original\n')
        self.assertEqual(
            {path.name: path.read_bytes() for path in archive.iterdir()},
            self.retained,
        )

    def test_interruption_after_new_backup_directory_is_resumable(self):
        self.interrupt_install_at(
            'after_backup_directory_create',
            'simulated directory interruption',
        )
        self.assert_retained_archive()
        self.assertEqual(list((self.state / 'backups').iterdir()), [])
        self.assertFalse((self.state / 'manifest.json').exists())
        self.assertEqual(self.arch.read_bytes(), b'second original\n')

        with contextlib.redirect_stdout(io.StringIO()):
            INSTALLER_MODULE.install()
        self.assertTrue((self.state / 'manifest.json').is_file())


class ManagedPathSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        self.env = {
            **os.environ,
            'HOME': str(self.home),
            'XDG_CONFIG_HOME': str(self.home / 'config'),
            'XDG_DATA_HOME': str(self.home / 'data'),
            'XDG_STATE_HOME': str(self.home / 'state'),
        }
        self.state = self.home / 'state/opencode-agentic/install'
        self.manifest = self.state / 'manifest.json'
        self.arch = self.home / 'config/opencode/agents/architect.md'

    def tearDown(self):
        self.tempdir.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(INSTALLER), *args],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_install_rejects_symlinked_agents_parent_without_external_write(self):
        external = self.home / 'external-agents'
        external.mkdir()
        marker = external / 'architect.md'
        marker.write_bytes(b'external untouched\n')
        agents = self.arch.parent
        agents.parent.mkdir(parents=True)
        agents.symlink_to(external, target_is_directory=True)

        result = self.run_cli('--install')

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('UNSAFE_PATH', result.stderr)
        self.assertEqual(marker.read_bytes(), b'external untouched\n')
        self.assertFalse(self.manifest.exists())

    def test_install_rejects_symlinked_backup_directory_without_external_write(self):
        external = self.home / 'external-backups'
        external.mkdir()
        marker = external / '3.bak'
        marker.write_bytes(b'external backup untouched\n')
        self.state.mkdir(parents=True)
        (self.state / 'backups').symlink_to(external, target_is_directory=True)
        self.arch.parent.mkdir(parents=True)
        self.arch.write_bytes(b'original architect\n')

        result = self.run_cli('--install')

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('UNSAFE_PATH', result.stderr)
        self.assertEqual(marker.read_bytes(), b'external backup untouched\n')
        self.assertEqual(self.arch.read_bytes(), b'original architect\n')
        self.assertFalse(self.manifest.exists())

    def test_install_rejects_symlinked_backup_archive_directory(self):
        external = self.home / 'external-archives'
        external.mkdir()
        marker = external / 'untouched'
        marker.write_bytes(b'external archive untouched\n')
        self.arch.parent.mkdir(parents=True)
        self.arch.write_bytes(b'first original\n')
        self.assertEqual(self.run_cli('--install').returncode, 0)
        self.assertEqual(self.run_cli('--rollback').returncode, 0)
        archive_root = self.state / 'backup-archives'
        archive_root.symlink_to(external, target_is_directory=True)
        self.arch.write_bytes(b'second original\n')

        result = self.run_cli('--install')

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('UNSAFE_PATH', result.stderr)
        self.assertEqual(marker.read_bytes(), b'external archive untouched\n')
        self.assertEqual(self.arch.read_bytes(), b'second original\n')
        self.assertFalse(self.manifest.exists())

    def test_rollback_rejects_symlinked_target_parent_without_external_write(self):
        self.arch.parent.mkdir(parents=True)
        self.arch.write_bytes(b'original architect\n')
        self.assertEqual(self.run_cli('--install').returncode, 0)
        installed = self.arch.read_bytes()
        detached = self.home / 'detached-agents'
        self.arch.parent.rename(detached)
        external = self.home / 'external-agents'
        external.mkdir()
        marker = external / 'architect.md'
        marker.write_bytes(installed)
        self.arch.parent.symlink_to(external, target_is_directory=True)

        result = self.run_cli('--rollback')

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('UNSAFE_PATH', result.stderr)
        self.assertEqual(marker.read_bytes(), installed)
        self.assertEqual((detached / 'architect.md').read_bytes(), installed)
        self.assertTrue(self.manifest.exists())

    def test_rollback_rejects_symlinked_backup_parent_without_external_read(self):
        self.arch.parent.mkdir(parents=True)
        self.arch.write_bytes(b'original architect\n')
        self.assertEqual(self.run_cli('--install').returncode, 0)
        installed = self.arch.read_bytes()
        backups = self.state / 'backups'
        detached = self.home / 'detached-backups'
        backups.rename(detached)
        external = self.home / 'external-backups'
        shutil.copytree(detached, external)
        snapshot = {path.name: path.read_bytes() for path in external.iterdir()}
        backups.symlink_to(external, target_is_directory=True)

        result = self.run_cli('--rollback')

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('UNSAFE_PATH', result.stderr)
        self.assertEqual(self.arch.read_bytes(), installed)
        self.assertEqual({path.name: path.read_bytes() for path in external.iterdir()}, snapshot)
        self.assertTrue(self.manifest.exists())

    def test_parent_swap_before_replace_never_writes_through_symlink(self):
        with mock.patch.dict(
            os.environ,
            {
                'HOME': str(self.home),
                'XDG_CONFIG_HOME': str(self.home / 'config'),
                'XDG_DATA_HOME': str(self.home / 'data'),
                'XDG_STATE_HOME': str(self.home / 'state'),
            },
        ):
            self.arch.parent.mkdir(parents=True)
            self.arch.write_bytes(b'original architect\n')
            external = self.home / 'external-agents'
            external.mkdir()
            marker = external / 'architect.md'
            marker.write_bytes(b'external untouched\n')
            detached = self.home / 'detached-agents'
            original_replace = INSTALLER_MODULE.os.replace
            swapped = False

            def swap_parent_before_replace(source, destination, *args, **kwargs):
                nonlocal swapped
                if not swapped and Path(destination).name == 'architect.md':
                    swapped = True
                    os.rename(self.arch.parent, detached)
                    self.arch.parent.symlink_to(external, target_is_directory=True)
                return original_replace(source, destination, *args, **kwargs)

            with (
                mock.patch.object(
                    INSTALLER_MODULE.os, 'replace', side_effect=swap_parent_before_replace
                ),
                self.assertRaisesRegex(RuntimeError, 'UNSAFE_PATH'),
            ):
                INSTALLER_MODULE.install()

            self.assertTrue(swapped)
            self.assertEqual(marker.read_bytes(), b'external untouched\n')


class ResumableRollbackTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        self.env = mock.patch.dict(
            os.environ,
            {
                'HOME': str(self.home),
                'XDG_CONFIG_HOME': str(self.home / 'config'),
                'XDG_DATA_HOME': str(self.home / 'data'),
                'XDG_STATE_HOME': str(self.home / 'state'),
            },
        )
        self.env.start()
        self.arch = self.home / 'config/opencode/agents/architect.md'
        self.arch.parent.mkdir(parents=True)
        self.arch.write_bytes(b'original architect\n')
        self.cli = self.home / 'config/opencode/cli.json'
        self.cli.write_bytes(b'{"animations": false}\n')
        self.original = {
            **{
                dest: (dest.read_bytes() if dest.exists() else None)
                for dest in INSTALLER_MODULE.targets()
            },
            self.cli: self.cli.read_bytes(),
        }
        with contextlib.redirect_stdout(io.StringIO()):
            INSTALLER_MODULE.install()
        self.plugin = self.home / 'config/opencode/plugins/agentic-kit/index.ts'
        self.manifest = self.home / 'state/opencode-agentic/install/manifest.json'
        self.backups = self.manifest.parent / 'backups'

    def tearDown(self):
        self.env.stop()
        self.tempdir.cleanup()

    def assert_original_state(self):
        for target, original in self.original.items():
            if original is None:
                self.assertFalse(target.exists(), f'{target} should be absent after rollback')
            else:
                self.assertTrue(target.is_file(), f'{target} should be restored as a file')
                self.assertEqual(target.read_bytes(), original)
        self.assertFalse(self.manifest.exists())

    def interrupt_at(self, stage, target=None):
        def checkpoint(actual_stage, actual_target=None):
            if actual_stage == stage and (target is None or actual_target == target):
                raise RuntimeError(f'simulated interruption at {stage}: {actual_target}')

        with (
            mock.patch.object(INSTALLER_MODULE, 'rollback_checkpoint', checkpoint, create=True),
            self.assertRaisesRegex(RuntimeError, 'simulated interruption'),
        ):
            INSTALLER_MODULE.rollback()
        state = json.loads(self.manifest.read_text())
        self.assertEqual(state['rollback']['status'], 'in_progress')
        self.assertTrue(self.backups.is_dir())

    def resume_and_assert_original(self):
        with contextlib.redirect_stdout(io.StringIO()):
            INSTALLER_MODULE.rollback()
        self.assert_original_state()

    def test_resume_after_restoring_preexisting_file(self):
        self.interrupt_at('after_target_change', self.arch)
        self.assertEqual(self.arch.read_bytes(), b'original architect\n')
        self.resume_and_assert_original()

    def test_resume_after_deleting_installer_created_file(self):
        self.interrupt_at('after_target_change', self.plugin)
        self.assertFalse(self.plugin.exists())
        self.resume_and_assert_original()

    def test_resume_between_restored_write_and_progress_record(self):
        self.interrupt_at('before_progress', self.arch)
        self.assertEqual(self.arch.read_bytes(), b'original architect\n')
        state = json.loads(self.manifest.read_text())
        self.assertNotIn(str(self.arch), state['rollback']['completed'])
        self.resume_and_assert_original()

    def test_resume_after_progress_record(self):
        self.interrupt_at('after_progress', self.arch)
        state = json.loads(self.manifest.read_text())
        self.assertIn(str(self.arch), state['rollback']['completed'])
        self.resume_and_assert_original()

    def test_install_during_in_progress_rollback_never_archives_backups(self):
        self.interrupt_at('after_progress', self.arch)
        backup_snapshot = {path.name: path.read_bytes() for path in self.backups.iterdir()}

        with self.assertRaisesRegex(RuntimeError, 'ROLLBACK_IN_PROGRESS'):
            INSTALLER_MODULE.install()

        self.assertEqual(
            {path.name: path.read_bytes() for path in self.backups.iterdir()},
            backup_snapshot,
        )
        self.assertFalse((self.manifest.parent / 'backup-archives').exists())
        self.assertTrue(self.manifest.is_file())

    def test_resume_before_final_manifest_removal(self):
        self.interrupt_at('before_manifest_remove')
        state = json.loads(self.manifest.read_text())
        expected = {str(path) for path in self.original}
        self.assertEqual(set(state['rollback']['completed']), expected)
        self.resume_and_assert_original()

    def test_local_change_after_interruption_is_never_overwritten(self):
        self.interrupt_at('after_progress', self.arch)
        self.arch.write_bytes(b'user edit after interruption\n')

        with self.assertRaisesRegex(RuntimeError, 'LOCAL_CHANGE'):
            INSTALLER_MODULE.rollback()

        self.assertEqual(self.arch.read_bytes(), b'user edit after interruption\n')
        self.assertTrue(self.manifest.is_file())
        self.assertTrue(self.backups.is_dir())

    def test_changed_backup_after_interruption_is_never_restored(self):
        self.interrupt_at('after_progress', self.arch)
        cli_installed = self.cli.read_bytes()
        (self.backups / 'cli.json.bak').write_bytes(b'{"unknown": true}\n')

        with self.assertRaisesRegex(RuntimeError, 'BACKUP_CHANGED'):
            INSTALLER_MODULE.rollback()

        self.assertEqual(self.cli.read_bytes(), cli_installed)
        self.assertTrue(self.manifest.is_file())

    def test_corrupted_backup_before_first_rollback_is_rejected_before_changes(self):
        installed = {target: target.read_bytes() for target in self.original}
        backup = Path(
            next(
                entry['backup']
                for entry in json.loads(self.manifest.read_text())['files']
                if entry['backup'] is not None
            )
        )
        backup.write_bytes(b'CORRUPTED-BACKUP')

        with self.assertRaisesRegex(RuntimeError, 'BACKUP_CHANGED'):
            INSTALLER_MODULE.rollback()

        for target, content in installed.items():
            self.assertEqual(target.read_bytes(), content)
        self.assertNotIn('rollback', json.loads(self.manifest.read_text()))
        self.assertEqual(backup.read_bytes(), b'CORRUPTED-BACKUP')

    def test_upgrade_preserves_trusted_hash_and_rejects_later_backup_corruption(self):
        before = json.loads(self.manifest.read_text())
        architect_entry = next(
            entry for entry in before['files'] if entry['dest'] == str(self.arch)
        )
        original_hash = architect_entry['backup_hash']
        original_source_bytes = INSTALLER_MODULE.source_bytes

        def upgraded_source(mapping):
            content = original_source_bytes(mapping)
            first = next(iter(mapping))
            content[first] += b'\n// test upgrade\n'
            return content

        with (
            mock.patch.object(INSTALLER_MODULE, 'source_bytes', upgraded_source),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            INSTALLER_MODULE.install(upgrade=True)
        after = json.loads(self.manifest.read_text())
        upgraded_entry = next(entry for entry in after['files'] if entry['dest'] == str(self.arch))
        self.assertEqual(upgraded_entry['backup_hash'], original_hash)
        Path(upgraded_entry['backup']).write_bytes(b'CORRUPTED-AFTER-UPGRADE')
        installed_arch = self.arch.read_bytes()

        with self.assertRaisesRegex(RuntimeError, 'BACKUP_CHANGED'):
            INSTALLER_MODULE.rollback()

        self.assertEqual(self.arch.read_bytes(), installed_arch)
        self.assertTrue(self.manifest.exists())

    def test_upgrade_does_not_invent_trust_for_legacy_backup(self):
        data = json.loads(self.manifest.read_text())
        architect_entry = next(entry for entry in data['files'] if entry['dest'] == str(self.arch))
        architect_entry.pop('backup_hash')
        self.manifest.write_text(json.dumps(data))
        original_source_bytes = INSTALLER_MODULE.source_bytes

        def upgraded_source(mapping):
            content = original_source_bytes(mapping)
            first = next(iter(mapping))
            content[first] += b'\n// legacy upgrade\n'
            return content

        with (
            mock.patch.object(INSTALLER_MODULE, 'source_bytes', upgraded_source),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            INSTALLER_MODULE.install(upgrade=True)

        upgraded = json.loads(self.manifest.read_text())
        upgraded_architect = next(
            entry for entry in upgraded['files'] if entry['dest'] == str(self.arch)
        )
        self.assertNotIn('backup_hash', upgraded_architect)
        installed_arch = self.arch.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'UNVERIFIED_BACKUP'):
            INSTALLER_MODULE.rollback()
        self.assertEqual(self.arch.read_bytes(), installed_arch)

    def test_manifest_records_trusted_hash_and_correct_backup_restores_original(self):
        data = json.loads(self.manifest.read_text())
        architect_entry = next(entry for entry in data['files'] if entry['dest'] == str(self.arch))
        self.assertEqual(
            architect_entry['backup_hash'], INSTALLER_MODULE.digest(b'original architect\n')
        )
        self.assertEqual(
            data['tui_config']['backup_hash'], INSTALLER_MODULE.digest(b'{"animations": false}\n')
        )
        self.resume_and_assert_original()

    def test_legacy_backup_without_trusted_hash_fails_closed(self):
        data = json.loads(self.manifest.read_text())
        for entry in [*data['files'], data['tui_config']]:
            entry.pop('backup_hash', None)
        self.manifest.write_text(json.dumps(data))
        installed_arch = self.arch.read_bytes()
        original_backup = next(
            Path(entry['backup']) for entry in data['files'] if entry['backup'] is not None
        )
        backup_content = original_backup.read_bytes()

        with self.assertRaisesRegex(RuntimeError, 'UNVERIFIED_BACKUP'):
            INSTALLER_MODULE.rollback()

        self.assertEqual(self.arch.read_bytes(), installed_arch)
        self.assertTrue(self.manifest.exists())
        self.assertEqual(original_backup.read_bytes(), backup_content)


class ResumableUninstallTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        self.environment = mock.patch.dict(
            os.environ,
            {
                'HOME': str(self.home),
                'XDG_CONFIG_HOME': str(self.home / 'config'),
                'XDG_DATA_HOME': str(self.home / 'data'),
                'XDG_STATE_HOME': str(self.home / 'state'),
            },
        )
        self.environment.start()
        self.architect = self.home / 'config/opencode/agents/architect.md'
        self.architect.parent.mkdir(parents=True)
        self.architect.write_text('original architect\n')
        with contextlib.redirect_stdout(io.StringIO()):
            INSTALLER_MODULE.install()
        self.manifest = self.home / 'state/opencode-agentic/install/manifest.json'
        self.backups = self.manifest.parent / 'backups'

    def tearDown(self):
        self.environment.stop()
        self.tempdir.cleanup()

    def interrupt_at(self, stage):
        def checkpoint(actual_stage, path=None):
            if actual_stage == stage:
                raise RuntimeError(f'simulated uninstall interruption at {stage}: {path}')

        with (
            mock.patch.object(INSTALLER_MODULE, 'uninstall_checkpoint', checkpoint),
            self.assertRaisesRegex(RuntimeError, 'simulated uninstall interruption'),
        ):
            INSTALLER_MODULE.uninstall()

    def resume_and_assert_uninstalled(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            INSTALLER_MODULE.uninstall()
        self.assertIn('UNINSTALLED: 0.8.1', output.getvalue())
        self.assertEqual(self.architect.read_text(), 'original architect\n')
        self.assertFalse(self.manifest.exists())
        self.assertFalse(self.backups.exists())
        self.assertFalse((self.home / 'config/opencode/plugins/agentic-kit/index.ts').exists())

    def test_resume_after_restoration_before_backup_cleanup(self):
        self.interrupt_at('after_cleanup_prepare')
        data = json.loads(self.manifest.read_text())
        self.assertEqual(data['uninstall']['status'], 'cleanup')
        self.assertTrue(self.backups.is_dir())
        self.resume_and_assert_uninstalled()

    def test_resume_after_removing_one_backup(self):
        self.interrupt_at('after_backup_remove')
        self.assertTrue(self.manifest.is_file())
        self.resume_and_assert_uninstalled()

    def test_resume_before_manifest_removal(self):
        self.interrupt_at('before_manifest_remove')
        self.assertTrue(self.manifest.is_file())
        self.assertFalse(self.backups.exists())
        self.resume_and_assert_uninstalled()

    def test_edit_after_interruption_is_not_overwritten_or_ignored(self):
        self.interrupt_at('after_cleanup_prepare')
        self.architect.write_text('edit after interrupted uninstall\n')

        with self.assertRaisesRegex(RuntimeError, 'LOCAL_CHANGE'):
            INSTALLER_MODULE.uninstall()

        self.assertEqual(self.architect.read_text(), 'edit after interrupted uninstall\n')
        self.assertTrue(self.manifest.exists())


class InstallerLockTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.home = Path(self.tempdir.name)
        self.env = {
            **os.environ,
            'HOME': str(self.home),
            'XDG_CONFIG_HOME': str(self.home / 'config'),
            'XDG_DATA_HOME': str(self.home / 'data'),
            'XDG_STATE_HOME': str(self.home / 'state'),
        }
        self.arch = self.home / 'config/opencode/agents/architect.md'
        self.arch.parent.mkdir(parents=True)
        self.arch.write_bytes(b'original architect\n')
        installed = self.run_cli('--install')
        self.assertEqual(installed.returncode, 0, installed.stderr)
        self.manifest = self.home / 'state/opencode-agentic/install/manifest.json'
        self.ready = self.home / 'rollback.ready'
        self.release = self.home / 'rollback.release'

    def tearDown(self):
        self.tempdir.cleanup()

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, str(INSTALLER), *args],
            env=self.env,
            text=True,
            capture_output=True,
            check=False,
        )

    def start_paused_rollback(self):
        code = f"""\
import importlib.util
import time
from pathlib import Path
spec = importlib.util.spec_from_file_location("installer", {str(INSTALLER)!r})
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
ready = Path({str(self.ready)!r})
release = Path({str(self.release)!r})
def checkpoint(stage, dest=None):
    if stage == "before_manifest_remove":
        ready.write_text("ready")
        while not release.exists():
            time.sleep(0.01)
installer.rollback_checkpoint = checkpoint
installer.rollback()
"""
        process = subprocess.Popen(
            [sys.executable, '-c', code],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        wait_for_path(self.ready, process)
        return process

    def test_concurrent_rollback_install_and_upgrade_fail_without_partial_changes(self):
        holder = self.start_paused_rollback()
        completed_manifest = self.manifest.read_bytes()
        backup_snapshot = {
            path.name: path.read_bytes() for path in (self.manifest.parent / 'backups').iterdir()
        }
        try:
            for args in [('--rollback',), ('--install',), ('--upgrade',), ('--uninstall',)]:
                with self.subTest(args=args):
                    contender = self.run_cli(*args)
                    self.assertNotEqual(contender.returncode, 0)
                    self.assertIn('INSTALLER_BUSY', contender.stderr)
                    self.assertEqual(self.manifest.read_bytes(), completed_manifest)
                    self.assertEqual(
                        {
                            path.name: path.read_bytes()
                            for path in (self.manifest.parent / 'backups').iterdir()
                        },
                        backup_snapshot,
                    )
        finally:
            self.release.write_text('continue')
            stdout, stderr = holder.communicate(timeout=5)
        self.assertEqual(holder.returncode, 0, f'{stdout}\n{stderr}')
        self.assertFalse(self.manifest.exists())

        reinstalled = self.run_cli('--install')
        self.assertEqual(reinstalled.returncode, 0, reinstalled.stderr)
        archives = list((self.manifest.parent / 'backup-archives').iterdir())
        self.assertEqual(len(archives), 1)
        self.assertEqual(
            {path.name: path.read_bytes() for path in archives[0].iterdir()},
            backup_snapshot,
            'completed rollback and a later install must archive original backups',
        )

    def test_process_exit_releases_lock(self):
        ready = self.home / 'lock.ready'
        code = f"""\
import importlib.util
import time
from pathlib import Path
spec = importlib.util.spec_from_file_location("installer", {str(INSTALLER)!r})
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
with installer.installer_lock():
    Path({str(ready)!r}).write_text("ready")
    while True:
        time.sleep(1)
"""
        holder = subprocess.Popen(
            [sys.executable, '-c', code],
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        wait_for_path(ready, holder)
        busy = self.run_cli('--install')
        self.assertNotEqual(busy.returncode, 0)
        self.assertIn('INSTALLER_BUSY', busy.stderr)
        holder.kill()
        holder.communicate(timeout=5)

        recovered = self.run_cli('--install')
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn('ALREADY_INSTALLED', recovered.stdout)

    def test_symlink_lock_file_is_rejected(self):
        lock_path = self.manifest.parent / 'installer.lock'
        lock_path.unlink(missing_ok=True)
        victim = self.home / 'victim'
        victim.write_text('untouched')
        lock_path.symlink_to(victim)

        result = self.run_cli('--install')

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('UNSAFE_LOCK', result.stderr)
        self.assertEqual(victim.read_text(), 'untouched')


if __name__ == '__main__':
    unittest.main()
