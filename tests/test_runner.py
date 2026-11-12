from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SRC = Path(__file__).resolve().parents[1] / 'runner/agentic_gate.py'
spec = importlib.util.spec_from_file_location('portable_gate', SRC)
assert spec and spec.loader
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'flat'
        self.root.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.root)], check=True)
        (self.root / 'tests').mkdir()
        (self.root / 'tests/test_example.py').write_text('def test_ok():\n    assert True\n')
        (self.root / 'example.py').write_text('def answer():\n    return 42\n')
        (self.root / '.agentic/drafts').mkdir(parents=True)
        (self.root / '.agentic/drafts/spec.md').write_text('Spec')
        (self.root / '.agentic/drafts/plan.md').write_text('Plan')
        self.state = Path(self.temp.name) / 'state'
        p = patch.dict(os.environ, {'AGENTIC_STATE_DIR': str(self.state)})
        p.start()
        self.addCleanup(p.stop)

    def test_flat_sources(self):
        self.assertEqual(m.source_targets(self.root), (['example.py'], ['example.py', 'tests']))

    def test_src_sources(self):
        (self.root / 'src').mkdir()
        (self.root / 'src/app.py').write_text('x=1\n')
        self.assertEqual(m.source_targets(self.root), (['src'], ['src', 'tests']))

    def test_git_root_enforced(self):
        m.validate_git_root(self.root)
        with self.assertRaisesRegex(m.GateError, 'PROJECT_NOT_GIT_ROOT'):
            m.validate_git_root(self.root / 'tests')

    def test_approve_check_modified_spec(self):
        with self.assertRaisesRegex(m.GateError, 'NO_APPROVAL'):
            m.validate_baseline(self.root)
        m.approve(self.root)
        self.assertIn('spec_sha256', m.validate_baseline(self.root))
        (self.root / 'specs/approved.md').write_text('Changed')
        with self.assertRaisesRegex(m.GateError, 'SPEC_CHANGED'):
            m.validate_baseline(self.root)

    def test_approve_atomicity_on_mid_write_failure(self):
        orig_open = Path.open

        def fail_second_document(path, mode='r', *args, **kwargs):
            if str(path).endswith('plans/approved.md') and mode == 'xb':
                raise OSError('simulated second document failure')
            return orig_open(path, mode, *args, **kwargs)

        with patch.object(Path, 'open', new=fail_second_document):
            with self.assertRaisesRegex(OSError, 'simulated'):
                m.approve(self.root)
        self.assertFalse(any(p.exists() for p in [*m.approved_paths(self.root),
                                                  *m.working_paths(self.root)]))
        m.approve(self.root)
        self.assertIn('spec_sha256', m.validate_baseline(self.root))

    def test_approve_rejects_dangling_symlink_target(self):
        approved = self.root / 'specs/approved.md'
        approved.parent.mkdir()
        approved.symlink_to(self.root / 'not-created')
        with self.assertRaisesRegex(m.GateError, 'EXISTING_APPROVED_PATH'):
            m.approve(self.root)
        self.assertFalse((self.root / 'plans/approved.md').exists())

    def test_approve_rejects_symlinked_parent(self):
        outside = Path(self.temp.name) / 'elsewhere'
        outside.mkdir()
        (self.root / 'plans').symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(m.GateError, 'SYMLINK_DIRECTORY'):
            m.approve(self.root)
        self.assertFalse(any(p.exists() for p in m.approved_paths(self.root)))

    def test_approve_rejects_nested_task_symlink(self):
        m.task_new(self.root, 'nested-link')
        outside = Path(self.temp.name) / 'task-drafts'
        outside.mkdir()
        tasks = self.root / '.agentic/tasks'
        tasks.parent.mkdir(exist_ok=True)
        tasks.symlink_to(outside, target_is_directory=True)
        drafts = m.draft_paths(self.root)
        drafts[0].parent.mkdir(parents=True)
        drafts[0].write_text('Spec')
        drafts[1].write_text('Plan')
        with self.assertRaisesRegex(m.GateError, 'SYMLINK_DIRECTORY'):
            m.approve(self.root)

    def test_receipt_staleness_and_corruption(self):
        m.approve(self.root)
        with patch.object(m, 'quality'):
            m.gate(self.root, 'fast')
        m.check_receipt(self.root, 'fast')
        (self.root / 'example.py').write_text('def answer(): return 43\n')
        with self.assertRaisesRegex(m.GateError, 'STALE_FAST'):
            m.check_receipt(self.root, 'fast')
        m.receipt_path(self.root, 'fast').write_text('{broken')
        with self.assertRaisesRegex(m.GateError, 'INVALID_RECEIPT'):
            m.check_receipt(self.root, 'fast')

    def test_failed_gate_invalidates_previous_receipt(self):
        m.approve(self.root)
        with patch.object(m, 'quality'):
            m.gate(self.root, 'fast')
        with patch.object(m, 'quality', side_effect=m.GateFailure('test failing')):
            with self.assertRaises(m.GateFailure):
                m.gate(self.root, 'fast')
        self.assertFalse(m.receipt_path(self.root, 'fast').exists())

    def test_tdd_evidence_enforces_order_and_exit_codes(self):
        m.approve(self.root)
        sample = self.root / 'tests/test_tdd_sample.py'
        sample.write_text('import unittest\n\nclass T(unittest.TestCase):\n    def test_it(self): self.fail("red")\n')
        command = [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests',
                   '-p', 'test_tdd_sample.py']
        fail = command
        m.record_tdd(self.root, 'answer', 'red', fail, 'new assertion fails before implementation')
        sample.write_text('import unittest\n\nclass T(unittest.TestCase):\n    def test_it(self): self.assertTrue(True)\n')
        m.record_tdd(self.root, 'answer', 'green', command)
        m.record_tdd(self.root, 'answer', 'refactor', command)
        data = json.loads(m.tdd_path(self.root).read_text())
        self.assertEqual([entry['stage'] for entry in data['cycles']['answer']],
                         ['red', 'green', 'refactor'])
        self.assertEqual(data['cycles']['answer'][0]['expected_failure'],
                         'new assertion fails before implementation')
        with self.assertRaisesRegex(m.GateError, 'TDD_ORDER'):
            m.record_tdd(self.root, 'other', 'green', command)
        with self.assertRaisesRegex(m.GateFailure, 'UNEXPECTED_EXIT'):
            m.record_tdd(self.root, 'bad-red', 'red', command, 'test should fail')
        self.assertNotIn('bad-red', json.loads(m.tdd_path(self.root).read_text())['cycles'])
        with self.assertRaisesRegex(m.GateFailure, 'expected 1, got 2'):
            m.record_tdd(self.root, 'cli-error', 'red',
                         [sys.executable, '-m', 'unittest', '--definitely-invalid'],
                         'an assertion should fail')
        with self.assertRaisesRegex(m.GateError, 'RED_EXPECTATION_REQUIRED'):
            m.record_tdd(self.root, 'missing-expectation', 'red', command)
        with self.assertRaisesRegex(m.GateError, 'TDD_COMMAND_DENIED'):
            m.record_tdd(self.root, 'proxy', 'red', [sys.executable, '-c', 'print("unsafe")'],
                         'proxy must not run')
        m.tdd_path(self.root).write_text('{broken')
        with self.assertRaisesRegex(m.GateError, 'INVALID_EVIDENCE'):
            m.evidence(self.root)

    def test_wheel_check_builds_installs_and_tests_outside_project(self):
        (self.root / 'pyproject.toml').write_text('[build-system]\nrequires=[]\nbuild-backend="x"\n')
        seen = []

        def fake_run(cwd, argv, **kwargs):
            seen.append((Path(cwd), argv, kwargs))
            if 'wheel' in argv and '--wheel-dir' in argv:
                dist = Path(argv[argv.index('--wheel-dir') + 1])
                (dist / 'example-1-py3-none-any.whl').write_bytes(b'wheel')
            return ''

        with patch.object(m, 'run', side_effect=fake_run):
            m.wheel_check(self.root, [sys.executable])
        self.assertEqual(len(seen), 3)
        self.assertTrue(all(cwd != self.root for cwd, _, _ in seen))
        self.assertIn('-c', seen[-1][1])
        self.assertEqual(seen[-1][1][-1], '/dev/null')
        self.assertEqual(seen[-1][2]['env']['PYTHONNOUSERSITE'], '1')

    def test_uv_wheel_check_is_offline_and_locked(self):
        (self.root / 'pyproject.toml').write_text('[build-system]\nrequires=[]\nbuild-backend="x"\n')
        seen = []

        def fake_run(cwd, argv, **kwargs):
            seen.append(argv)
            if '--wheel' in argv and '--out-dir' in argv:
                dist = Path(argv[argv.index('--out-dir') + 1])
                (dist / 'example-1-py3-none-any.whl').write_bytes(b'wheel')
            return ''

        with patch.object(m, 'run', side_effect=fake_run):
            m.wheel_check(self.root, ['uv', 'run', '--locked'])
        self.assertIn('--no-build-isolation', seen[0])
        self.assertIn('--offline', seen[0])
        self.assertIn('--no-python-downloads', seen[0])
        self.assertIn('--offline', seen[1])
        self.assertEqual(seen[2][:4], ['uv', 'run', '--locked', 'python'])

    def test_fast_commands_in_flat_layout(self):
        m.approve(self.root)
        seen = []
        with patch.object(m, 'require_module'), patch.object(m, 'run', side_effect=lambda p, argv, **kw: seen.append(argv)):
            m.quality(self.root, 'fast')
        self.assertEqual(len(seen), 8)
        self.assertTrue(any('--version' in argv for argv in seen))
        self.assertTrue(any('example.py' in argv for argv in seen))
        self.assertTrue(any('tests' in argv for argv in seen))
        self.assertTrue(any('pytest' in argv for argv in seen))

    def test_no_lock_no_install(self):
        backend, python = m.python_backend(self.root)
        self.assertEqual(backend[1:], ['-m'])
        self.assertTrue(python[0].startswith('/'))
        self.assertFalse((self.root / 'uv.lock').exists())

    def test_cli_non_repo_blocked(self):
        result = subprocess.run(['python3', str(SRC), '--project', self.temp.name, 'check-approval'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('NOT_GIT_REPOSITORY', result.stderr)

    def test_state_is_separate_for_independent_projects(self):
        other = Path(self.temp.name) / 'other' / self.root.name
        other.mkdir(parents=True)
        subprocess.run(['git', 'init', '-q', str(other)], check=True)
        (other / '.agentic/drafts').mkdir(parents=True)
        (other / '.agentic/drafts/spec.md').write_text('Other spec')
        (other / '.agentic/drafts/plan.md').write_text('Other plan')

        m.approve(self.root)
        m.approve(other)

        first = m.state_dir(self.root)
        second = m.state_dir(other)
        self.assertNotEqual(first, second)
        self.assertTrue((first / 'baseline/spec.md').is_file())
        self.assertTrue((second / 'baseline/spec.md').is_file())

    def test_sequential_tasks_keep_approval_receipts_and_history_separate(self):
        m.task_new(self.root, 'task-a')
        a_drafts = m.draft_paths(self.root)
        a_drafts[0].parent.mkdir(parents=True)
        a_drafts[0].write_text('Task A spec')
        a_drafts[1].write_text('Task A plan')
        m.approve(self.root)
        with patch.object(m, 'quality'):
            m.gate(self.root, 'full')
        a_receipt = m.receipt_path(self.root, 'full')
        a_baseline = m.approved_paths(self.root)
        self.assertEqual(json.loads(a_receipt.read_text())['task_id'], 'task-a')
        m.complete_task(self.root)

        m.task_new(self.root, 'task-b')
        with self.assertRaisesRegex(m.GateError, 'NO_APPROVAL'):
            m.validate_baseline(self.root)
        with self.assertRaisesRegex(m.GateError, 'NO_APPROVAL'):
            m.quality(self.root, 'fast')

        b_receipt = m.receipt_path(self.root, 'full')
        b_receipt.parent.mkdir(parents=True)
        b_receipt.write_bytes(a_receipt.read_bytes())
        with self.assertRaisesRegex(m.GateError, 'WRONG_TASK_RECEIPT'):
            m.check_receipt(self.root, 'full')

        b_drafts = m.draft_paths(self.root)
        b_drafts[0].parent.mkdir(parents=True)
        b_drafts[0].write_text('Task B spec')
        b_drafts[1].write_text('Task B plan')
        m.approve(self.root)
        self.assertNotEqual(m.approved_paths(self.root), a_baseline)
        m.approved_paths(self.root)[0].write_text('corrupt B only')
        with self.assertRaisesRegex(m.GateError, 'SPEC_CHANGED'):
            m.validate_baseline(self.root)

        m.task_use(self.root, 'task-a')
        self.assertEqual(m.validate_baseline(self.root)['task_id'], 'task-a')
        self.assertEqual(m.approved_paths(self.root), a_baseline)
        registry = json.loads(m.registry_path(self.root).read_text())
        self.assertIsNotNone(registry['tasks']['task-a']['completed_at'])
        self.assertIn('task-b', registry['tasks'])

    def test_legacy_state_is_adopted_without_moving_history(self):
        m.approve(self.root)
        legacy_baseline = m.state_dir(self.root) / 'baseline/spec.md'
        self.assertTrue(legacy_baseline.is_file())
        registry = m.load_registry(self.root)
        self.assertEqual(registry['active_task'], 'legacy')
        self.assertEqual(registry['tasks']['legacy']['layout'], 'legacy')
        self.assertEqual(m.approved_paths(self.root)[0], legacy_baseline)
        with patch.object(m, 'quality'):
            m.gate(self.root, 'full')
        receipt = json.loads(m.receipt_path(self.root, 'full').read_text())
        receipt.pop('task_id')
        receipt['baseline'].pop('task_id')
        m.receipt_path(self.root, 'full').write_text(json.dumps(receipt))
        m.check_receipt(self.root, 'full')


if __name__ == '__main__':
    unittest.main()
