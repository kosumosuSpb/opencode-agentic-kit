from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import subprocess
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

    def test_fast_commands_in_flat_layout(self):
        m.approve(self.root)
        seen = []
        with patch.object(m, 'require_module'), patch.object(m, 'run', side_effect=lambda p, argv, **kw: seen.append(argv)):
            m.quality(self.root, 'fast')
        self.assertEqual(len(seen), 4)
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


if __name__ == '__main__':
    unittest.main()
