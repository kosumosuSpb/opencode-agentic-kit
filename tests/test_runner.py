from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from runner.agentic import cli as m
from runner.agentic.coverage import coverage_source_files, validate_lcov
from runner.agentic.wheel import WheelVerifier

SRC = Path(__file__).resolve().parents[1] / 'runner/agentic_gate.py'


def passing_quality(_project: Path, level: str, _runner=None) -> dict[str, object]:
    if level == 'fast':
        return {'applicable': None, 'status': 'not_run'}
    return {'applicable': False, 'status': 'not_applicable'}


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
        self.assertEqual(
            m.source_targets(self.root), (['example.py'], ['example.py', 'tests/test_example.py'])
        )

    def test_src_sources(self):
        (self.root / 'src').mkdir()
        (self.root / 'src/app.py').write_text('x=1\n')
        self.assertEqual(
            m.source_targets(self.root),
            (['example.py', 'src/app.py'], ['example.py', 'src/app.py', 'tests/test_example.py']),
        )

    def test_sources_include_nested_scripts_packages_and_mixed_layout(self):
        (self.root / 'app').mkdir()
        (self.root / 'app/__init__.py').write_text('VALUE = 1\n')
        (self.root / 'app/service.py').write_text('VALUE = 2\n')
        (self.root / 'runner').mkdir()
        (self.root / 'runner/agentic_gate.py').write_text('VALUE = 3\n')
        (self.root / 'tools/nested').mkdir(parents=True)
        (self.root / 'tools/nested/__init__.py').write_text('VALUE = 4\n')
        (self.root / 'tools/nested/helper.py').write_text('VALUE = 5\n')

        sources, lint = m.source_targets(self.root)

        self.assertEqual(
            sources,
            [
                'app/__init__.py',
                'app/service.py',
                'example.py',
                'runner/agentic_gate.py',
                'tools/nested/__init__.py',
                'tools/nested/helper.py',
            ],
        )
        self.assertEqual(lint, sorted([*sources, 'tests/test_example.py']))

    def test_test_files_are_linted_but_not_production_targets(self):
        (self.root / 'test_root.py').write_text('def test_root():\n    assert True\n')
        (self.root / 'conftest.py').write_text('VALUE = 1\n')

        sources, lint = m.source_targets(self.root)

        self.assertNotIn('test_root.py', sources)
        self.assertNotIn('conftest.py', sources)
        self.assertIn('test_root.py', lint)
        self.assertIn('conftest.py', lint)

    def test_sources_exclude_generated_directories_without_hiding_similar_names(self):
        excluded = [
            '.git',
            '.venv',
            'venv',
            '__pycache__',
            '.tox',
            '.mypy_cache',
            '.ruff_cache',
            '.pytest_cache',
            'node_modules',
            'build',
            'dist',
            '.agentic',
        ]
        for directory in excluded:
            path = self.root / 'nested' / directory
            path.mkdir(parents=True, exist_ok=True)
            (path / 'ignored.py').write_text('IGNORED = True\n')
        (self.root / 'nested/build_tools').mkdir(parents=True)
        (self.root / 'nested/build_tools/kept.py').write_text('KEPT = True\n')

        sources, _lint = m.source_targets(self.root)

        self.assertIn('nested/build_tools/kept.py', sources)
        self.assertFalse(any(path.endswith('ignored.py') for path in sources))

    def test_sources_are_deduplicated_deterministic_and_do_not_follow_symlinks(self):
        (self.root / 'zeta').mkdir()
        (self.root / 'zeta/script.py').write_text('VALUE = 1\n')
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        (outside / 'escaped.py').write_text('ESCAPED = True\n')
        try:
            (self.root / 'linked-outside').symlink_to(outside, target_is_directory=True)
            (self.root / 'linked-script.py').symlink_to(self.root / 'zeta/script.py')
        except OSError as exc:
            self.skipTest(f'symlinks unavailable: {exc}')

        first, _lint = m.source_targets(self.root)
        second, _lint = m.source_targets(self.root)

        self.assertEqual(first, second)
        self.assertEqual(first, sorted(set(first)))
        self.assertNotIn('linked-script.py', first)
        self.assertFalse(any('linked-outside' in path for path in first))

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

    def test_approve_normalizes_status_but_preserves_drafts(self):
        spec, plan = m.draft_paths(self.root)
        spec.write_text('# Spec\n\n**Статус:** DRAFT — ожидает решения человека\n\nRequirement.\n')
        plan.write_text('# Plan\n\n**Статус:** DRAFT\n\nSteps.\n')
        original = (spec.read_bytes(), plan.read_bytes())

        m.approve(self.root)

        approved = m.working_paths(self.root)
        for path in approved:
            self.assertIn(m.APPROVED_STATUS, path.read_text().splitlines())
            self.assertNotIn('DRAFT', path.read_text())
        self.assertEqual((spec.read_bytes(), plan.read_bytes()), original)
        self.assertEqual(
            [p.read_bytes() for p in approved],
            [p.read_bytes() for p in m.approved_paths(self.root)],
        )

    def test_approve_rejects_pending_human_approval_prose(self):
        spec, plan = m.draft_paths(self.root)
        spec.write_text('# Spec\n\n**Статус:** DRAFT\n\nРешение требует утверждения человеком.\n')
        plan.write_text('Plan')
        with self.assertRaisesRegex(m.GateError, 'PENDING_APPROVAL_TEXT'):
            m.approve(self.root)
        self.assertFalse(
            any(
                path.exists()
                for path in (*m.approved_paths(self.root), *m.working_paths(self.root))
            )
        )

    def test_approve_rejects_self_negating_draft_disclaimer_without_rewriting_meaning(self):
        spec, plan = m.draft_paths(self.root)
        original = (
            '# Spec\n\n**Статус:** DRAFT\n\n'
            'Этот документ является черновиком и не утверждает требования самостоятельно.\n'
        )
        spec.write_text(original)
        plan.write_text('Plan')

        with self.assertRaisesRegex(m.GateError, 'PENDING_APPROVAL_TEXT'):
            m.approve(self.root)

        self.assertEqual(spec.read_text(), original)
        self.assertFalse(
            any(
                path.exists()
                for path in (*m.approved_paths(self.root), *m.working_paths(self.root))
            )
        )

    def test_human_reapproval_archives_prior_approval_and_evidence(self):
        m.approve(self.root)
        old = m.validate_baseline(self.root)
        m.tdd_path(self.root).parent.mkdir(parents=True, exist_ok=True)
        m.tdd_path(self.root).write_text(json.dumps({'cycles': {'old': []}}))
        m.receipt_path(self.root, 'verify').write_text(json.dumps({'old': True}))
        spec, plan = m.draft_paths(self.root)
        spec.write_text(
            '# Revised spec\n\n**Статус:** DRAFT\n\nSame requirement, clearer wording.\n'
        )
        plan.write_text('# Revised plan\n\n**Статус:** DRAFT\n\nSame steps.\n')

        m.approve(self.root, replace_existing=True)

        new = m.validate_baseline(self.root)
        self.assertNotEqual(old, new)
        archives = list((m.task_state_dir(self.root) / 'approval-history').iterdir())
        self.assertEqual(len(archives), 1)
        manifest = json.loads((archives[0] / 'revision.json').read_text())
        self.assertEqual(manifest['approval'], old)
        self.assertTrue((archives[0] / 'tdd.json').is_file())
        self.assertTrue((archives[0] / 'verify.json').is_file())
        self.assertEqual(m.sha(archives[0] / 'baseline-spec.md'), old['spec_sha256'])

    def test_human_reapproval_rotates_tdd_and_allows_new_evidence(self):
        m.approve(self.root)
        old = m.validate_baseline(self.root)
        failing_test = self.root / 'tests/test_tdd_case.py'
        failing_test.write_text(
            'import unittest\n\n'
            'class TDDCase(unittest.TestCase):\n'
            '    def test_behavior(self):\n'
            '        self.fail("expected old failure")\n'
        )
        command = [
            sys.executable,
            '-m',
            'unittest',
            'discover',
            '-s',
            'tests',
            '-p',
            'test_tdd_case.py',
        ]
        m.record_tdd(self.root, 'old-cycle', 'red', command, 'expected old failure')
        old_tdd = m.tdd_path(self.root).read_bytes()
        spec, plan = m.draft_paths(self.root)
        spec.write_text('# Revised spec\n\n**Статус:** DRAFT\n\nNew requirement.\n')
        plan.write_text('# Revised plan\n\n**Статус:** DRAFT\n\nNew steps.\n')

        m.approve(self.root, replace_existing=True)

        new = m.validate_baseline(self.root)
        self.assertNotEqual(old, new)
        archives = list((m.task_state_dir(self.root) / 'approval-history').iterdir())
        self.assertEqual(len(archives), 1)
        self.assertEqual((archives[0] / 'tdd.json').read_bytes(), old_tdd)
        self.assertFalse(m.tdd_path(self.root).exists())
        failing_test.write_text(
            failing_test.read_text().replace('expected old failure', 'distinct new failure')
        )
        m.record_tdd(self.root, 'new-cycle', 'red', command, 'distinct new failure')
        active = json.loads(m.tdd_path(self.root).read_text())
        self.assertEqual(active['baseline'], new)
        self.assertEqual(set(active['cycles']), {'new-cycle'})

    def test_identical_reapproval_keeps_receipt_and_review_current(self):
        m.approve(self.root)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'verify')
        review = m.begin_review(self.root, 'verify')
        m.submit_review(self.root, review['review_id'], 'pass', [])
        spec, plan = m.draft_paths(self.root)
        approved_spec, approved_plan = m.working_paths(self.root)
        spec.write_bytes(approved_spec.read_bytes())
        plan.write_bytes(approved_plan.read_bytes())

        m.approve(self.root, replace_existing=True)

        self.assertEqual(m.current_receipt(self.root, 'verify')['passed'], True)
        reused = m.begin_review(self.root, 'verify')
        self.assertFalse(reused['created'])
        self.assertEqual(reused['review_id'], review['review_id'])
        self.assertEqual(reused['status'], 'passed')
        self.assertFalse((m.task_state_dir(self.root) / 'approval-history').exists())

    def test_reapproval_lifecycle_uses_only_new_tdd_and_current_receipts(self):
        m.approve(self.root)
        old_baseline = m.validate_baseline(self.root)
        sample = self.root / 'tests/test_reapproval_flow.py'
        sample.write_text(
            'import unittest\n\n'
            'class ReapprovalFlow(unittest.TestCase):\n'
            '    def test_behavior(self):\n'
            '        self.fail("old baseline failure")\n'
        )
        command = [
            sys.executable,
            '-m',
            'unittest',
            'discover',
            '-s',
            'tests',
            '-p',
            'test_reapproval_flow.py',
        ]
        m.record_tdd(self.root, 'old-baseline', 'red', command, 'old baseline failure')
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'verify')
        old_review = m.begin_review(self.root, 'verify')
        m.submit_review(self.root, old_review['review_id'], 'pass', [])
        m._write_json_atomic(
            m.task_state_dir(self.root) / 'acceptance-checks.json',
            {
                'version': 1,
                'entries': [
                    {
                        'criterion': 'AC1',
                        'baseline': old_baseline,
                        'fingerprint': m.fingerprint(self.root),
                        'exit_code': 0,
                    }
                ],
            },
        )
        spec, plan = m.draft_paths(self.root)
        spec.write_text('# Revised spec\n\n**Статус:** DRAFT\n\nNew requirement.\n')
        plan.write_text('# Revised plan\n\n**Статус:** DRAFT\n\nNew steps.\n')

        m.approve(self.root, replace_existing=True)

        with self.assertRaisesRegex(m.GateError, 'STALE_VERIFY_RECEIPT'):
            m.current_receipt(self.root, 'verify')
        sample.write_text(
            sample.read_text().replace('old baseline failure', 'distinct new baseline failure')
        )
        m.record_tdd(
            self.root,
            'new-baseline',
            'red',
            command,
            'distinct new baseline failure',
        )
        sample.write_text(
            'import unittest\n\n'
            'class ReapprovalFlow(unittest.TestCase):\n'
            '    def test_behavior(self):\n'
            '        self.assertEqual(2 + 2, 4)\n'
        )
        m.record_tdd(self.root, 'new-baseline', 'green', command)
        m.record_tdd(self.root, 'new-baseline', 'refactor', command)
        evidence = m.collect_evidence(self.root)
        self.assertEqual(set(evidence['tdd']['cycles']), {'new-baseline'})
        active_entries = evidence['tdd']['cycles']['new-baseline']
        self.assertEqual(
            [entry['stage'] for entry in active_entries], ['red', 'green', 'refactor']
        )
        self.assertNotEqual(evidence['current_baseline'], old_baseline)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'verify')
        context = m._evidence_service(self.root).review_context('verify')
        self.assertTrue(context['tdd']['cycles']['new-baseline']['complete'])
        self.assertFalse(context['acceptance_checks']['records'][0]['current_pass'])
        review = m.begin_review(self.root, 'verify')
        self.assertEqual(review['mode'], 'independent-review')
        m.submit_review(self.root, review['review_id'], 'pass', [])
        superseded_binding = json.loads(
            m.review_path(self.root, old_review['review_id']).read_text()
        )['binding']
        self.assertNotEqual(superseded_binding['approval'], m.validate_baseline(self.root))
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'full')
        final = m.begin_review(self.root, 'finalize')
        self.assertEqual(final['mode'], 'reuse-independent-review')
        m.submit_review(self.root, final['review_id'], 'pass', [])

        accepted = m.accept_review(self.root, final['review_id'])

        self.assertFalse(accepted['already_completed'])
        self.assertEqual(accepted['approval'], m.validate_baseline(self.root))

    def test_implement_context_contains_approval_paths_and_toolchain(self):
        m.approve(self.root)
        probes = [
            subprocess.CompletedProcess(
                ['/venv/bin/python', '--version'], 0, stdout='Python 3.12.7\n', stderr=''
            ),
            subprocess.CompletedProcess(
                ['/venv/bin/python', '-m', 'pytest', '--version'],
                0,
                stdout='pytest 8.3.5\n',
                stderr='',
            ),
        ]
        output = io.StringIO()
        with (
            patch.object(
                m, 'python_backend', return_value=(['/venv/bin/python', '-m'], ['/venv/bin/python'])
            ),
            patch.object(m.subprocess, 'run', side_effect=probes),
            redirect_stdout(output),
        ):
            m.implement_context(self.root)
        context = json.loads(output.getvalue())
        self.assertEqual(context['task_id'], 'legacy')
        self.assertEqual(context['approved_spec'], 'specs/approved.md')
        self.assertEqual(context['approved_plan'], 'plans/approved.md')
        self.assertTrue(context['approval']['verified'])
        self.assertEqual(
            context['python']['test_command'], ['/venv/bin/python', '-m', 'pytest', '-q']
        )
        self.assertTrue(context['python']['python_probe']['available'])
        self.assertTrue(context['python']['pytest_probe']['available'])

    def test_approve_atomicity_on_mid_write_failure(self):
        orig_open = Path.open

        def fail_second_document(path, mode='r', *args, **kwargs):
            if str(path).endswith('plans/approved.md') and mode == 'xb':
                raise OSError('simulated second document failure')
            return orig_open(path, mode, *args, **kwargs)

        with (
            patch.object(Path, 'open', new=fail_second_document),
            self.assertRaisesRegex(OSError, 'simulated'),
        ):
            m.approve(self.root)
        self.assertFalse(
            any(p.exists() for p in [*m.approved_paths(self.root), *m.working_paths(self.root)])
        )
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
        outside = Path(self.temp.name) / 'task-drafts'
        outside.mkdir()
        tasks = self.root / '.agentic/tasks'
        tasks.parent.mkdir(exist_ok=True)
        tasks.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(m.GateError, 'SYMLINK_DIRECTORY'):
            m.task_new(self.root, 'nested-link')

    def test_receipt_staleness_and_corruption(self):
        m.approve(self.root)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'fast')
        m.check_receipt(self.root, 'fast')
        (self.root / 'example.py').write_text('def answer(): return 43\n')
        with self.assertRaisesRegex(m.GateError, 'STALE_FAST'):
            m.check_receipt(self.root, 'fast')
        m.receipt_path(self.root, 'fast').write_text('{broken')
        with self.assertRaisesRegex(m.GateError, 'INVALID_RECEIPT'):
            m.check_receipt(self.root, 'fast')

    def test_review_context_reports_tdd_and_verifiable_wheel_steps(self):
        m.approve(self.root)
        baseline = m.validate_baseline(self.root)
        m.tdd_path(self.root).write_text(
            json.dumps(
                {
                    'baseline': baseline,
                    'cycles': {
                        'answer': [
                            {
                                'stage': 'red',
                                'command': ['python3', '-m', 'pytest'],
                                'exit_code': 1,
                                'expected_failure': 'missing answer',
                                'recorded_at': '2026-01-01T00:00:00Z',
                            },
                            {
                                'stage': 'green',
                                'command': ['python3', '-m', 'pytest'],
                                'exit_code': 0,
                                'recorded_at': '2026-01-01T00:01:00Z',
                            },
                            {
                                'stage': 'refactor',
                                'command': ['python3', '-m', 'pytest'],
                                'exit_code': 0,
                                'recorded_at': '2026-01-01T00:02:00Z',
                            },
                        ],
                    },
                }
            )
        )
        scratch = Path(self.temp.name) / 'wheel-scratch'
        commands = [
            {
                'cwd': str(scratch),
                'command': [
                    sys.executable,
                    '-m',
                    'pip',
                    'wheel',
                    '--wheel-dir',
                    'dist',
                    str(self.root),
                ],
                'exit_code': 0,
            },
            {'kind': 'wheel_artifact', 'path': 'example.whl', 'sha256': 'abc'},
            {
                'cwd': str(scratch),
                'command': [
                    sys.executable,
                    '-m',
                    'pip',
                    'install',
                    '--target',
                    'site',
                    'example.whl',
                ],
                'exit_code': 0,
            },
            {
                'kind': 'wheel_import_provenance',
                'cwd': str(scratch),
                'command': [sys.executable, '-I', '-S', '-c', 'probe'],
                'exit_code': 0,
                'source_root_excluded': True,
                'imports': ['example'],
            },
            {
                'kind': 'wheel_public_api_smoke',
                'cwd': str(scratch),
                'command': [sys.executable, '-I', '-S', '-c', 'smoke'],
                'exit_code': 0,
                'source_root_excluded': True,
                'import_names': ['example'],
            },
        ]
        receipt = {
            'task_id': 'legacy',
            'level': 'verify',
            'fingerprint': m.fingerprint(self.root),
            'baseline': baseline,
            'commands': commands,
            'wheel_verified': True,
            'passed_at': '2026-01-01T00:03:00Z',
        }
        m.receipt_path(self.root, 'verify').write_text(json.dumps(receipt))
        output = io.StringIO()
        with redirect_stdout(output):
            m.review_context(self.root, 'verify')
        context = json.loads(output.getvalue())
        self.assertEqual(context['active_task'], 'legacy')
        self.assertTrue(context['tdd']['cycles']['answer']['complete'])
        self.assertTrue(context['wheel']['verified'])
        self.assertEqual(context['wheel']['artifact'][0]['sha256'], 'abc')
        self.assertTrue(Path(context['receipt']['source']).is_file())

    def test_review_context_rejects_missing_or_stale_receipt(self):
        m.approve(self.root)
        with self.assertRaisesRegex(m.GateError, 'NO_VERIFY_RECEIPT'):
            m.review_context(self.root, 'verify')
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'verify')
        (self.root / 'example.py').write_text('changed = True\n')
        with self.assertRaisesRegex(m.GateError, 'STALE_VERIFY_RECEIPT'):
            m.review_context(self.root, 'verify')

    def test_review_context_does_not_invent_missing_tdd_or_wheel_evidence(self):
        m.approve(self.root)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'verify')
        output = io.StringIO()
        with redirect_stdout(output):
            m.review_context(self.root, 'verify')
        context = json.loads(output.getvalue())
        self.assertEqual(context['tdd']['cycles'], {})
        self.assertFalse(context['wheel']['verified'])
        self.assertEqual(context['wheel']['artifact'], [])

    def test_failed_gate_invalidates_previous_receipt(self):
        m.approve(self.root)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'fast')
        with (
            patch.object(m, 'quality', side_effect=m.GateFailure('test failing')),
            self.assertRaises(m.GateFailure),
        ):
            m.gate(self.root, 'fast')
        self.assertFalse(m.receipt_path(self.root, 'fast').exists())

    def test_approval_preflight_failure_preserves_prior_receipt_for_audit(self):
        m.approve(self.root)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'verify')
        receipt = m.receipt_path(self.root, 'verify')
        before = receipt.read_bytes()
        m.working_paths(self.root)[0].write_text('invalid approval status\n')
        m.approved_paths(self.root)[0].write_text('invalid approval status\n')

        with self.assertRaisesRegex(m.GateError, 'APPROVAL_STATUS_INVALID'):
            m.gate(self.root, 'verify')

        self.assertEqual(receipt.read_bytes(), before)

    def test_fingerprint_ignores_only_generated_artifacts(self):
        m.approve(self.root)
        before = m.fingerprint(self.root)
        generated = {
            '.coverage': 'coverage',
            '.metrics/dry.edn': 'metrics',
            'target/coverage/lcov.info': 'lcov',
            'build/lib/example.py': 'generated',
            '__pycache__/example.pyc': 'bytecode',
            'sample.egg-info/PKG-INFO': 'metadata',
            'src/sample.egg-info/PKG-INFO': 'metadata',
        }
        for relative, content in generated.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.assertEqual(m.fingerprint(self.root), before)

        meaningful = self.root / 'pkg/looks.egg-info/source.py'
        meaningful.parent.mkdir(parents=True)
        meaningful.write_text('source = True\n')
        self.assertNotEqual(
            m.fingerprint(self.root),
            before,
            'nested source directories must not be hidden as build metadata',
        )

    def test_fingerprint_detects_sources_tests_config_approved_symlink_and_new_files(self):
        m.approve(self.root)
        before = m.fingerprint(self.root)
        candidates = [
            self.root / 'example.py',
            self.root / 'tests/test_example.py',
            self.root / 'pyproject.toml',
            m.working_paths(self.root)[0],
        ]
        for path in candidates:
            existed = path.exists()
            original = path.read_bytes() if existed else None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('meaningful change\n')
            self.assertNotEqual(m.fingerprint(self.root), before, str(path))
            if existed:
                path.write_bytes(original)
            else:
                path.unlink()
        link = self.root / 'new-link.py'
        link.symlink_to('example.py')
        self.assertNotEqual(m.fingerprint(self.root), before)
        link.unlink()
        (self.root / 'new.py').write_text('new = True\n')
        self.assertNotEqual(m.fingerprint(self.root), before)

    def test_gate_rejects_meaningful_change_during_quality(self):
        m.approve(self.root)

        def mutate(*_args):
            (self.root / 'example.py').write_text('changed_during_gate = True\n')

        with (
            patch.object(m, 'quality', side_effect=mutate),
            self.assertRaisesRegex(m.GateError, 'WORKTREE_CHANGED_DURING_GATE'),
        ):
            m.gate(self.root, 'verify')
        self.assertFalse(m.receipt_path(self.root, 'verify').exists())

    def test_tdd_evidence_enforces_order_and_exit_codes(self):
        m.approve(self.root)
        sample = self.root / 'tests/test_tdd_sample.py'
        sample.write_text(
            'import unittest\n\nclass T(unittest.TestCase):\n    def test_it(self): self.fail("red")\n'
        )
        command = [
            sys.executable,
            '-m',
            'unittest',
            'discover',
            '-s',
            'tests',
            '-p',
            'test_tdd_sample.py',
        ]
        fail = command
        m.record_tdd(self.root, 'answer', 'red', fail, 'new assertion fails before implementation')
        sample.write_text(
            'import unittest\n\nclass T(unittest.TestCase):\n    def test_it(self): self.assertTrue(True)\n'
        )
        m.record_tdd(self.root, 'answer', 'green', command)
        m.record_tdd(self.root, 'answer', 'refactor', command)
        data = json.loads(m.tdd_path(self.root).read_text())
        self.assertEqual(
            [entry['stage'] for entry in data['cycles']['answer']], ['red', 'green', 'refactor']
        )
        self.assertEqual(
            data['cycles']['answer'][0]['expected_failure'],
            'new assertion fails before implementation',
        )
        with self.assertRaisesRegex(m.GateError, 'TDD_ORDER'):
            m.record_tdd(self.root, 'other', 'green', command)
        with self.assertRaisesRegex(m.GateFailure, 'UNEXPECTED_EXIT'):
            m.record_tdd(self.root, 'bad-red', 'red', command, 'test should fail')
        self.assertNotIn('bad-red', json.loads(m.tdd_path(self.root).read_text())['cycles'])
        with self.assertRaisesRegex(m.GateFailure, 'expected 1, got 2'):
            m.record_tdd(
                self.root,
                'cli-error',
                'red',
                [sys.executable, '-m', 'unittest', '--definitely-invalid'],
                'an assertion should fail',
            )
        with self.assertRaisesRegex(m.GateError, 'RED_EXPECTATION_REQUIRED'):
            m.record_tdd(self.root, 'missing-expectation', 'red', command)
        with self.assertRaisesRegex(m.GateError, 'TDD_COMMAND_DENIED'):
            m.record_tdd(
                self.root,
                'proxy',
                'red',
                [sys.executable, '-c', 'print("unsafe")'],
                'proxy must not run',
            )
        m.tdd_path(self.root).write_text('{broken')
        with self.assertRaisesRegex(m.GateError, 'INVALID_EVIDENCE'):
            m.evidence(self.root)

    def test_tdd_does_not_execute_arbitrary_path_named_pytest_python_or_uv(self):
        """The auto-allowed TDD runner is not a generic executable proxy."""
        for command in (
            ['./pytest', '-q', 'tests'],
            ['/tmp/untrusted/pytest', '-q', 'tests'],
            ['./python3', '-m', 'pytest', 'tests'],
            ['/tmp/untrusted/python3', '-m', 'pytest', 'tests'],
            ['../uv', 'run', '--locked', 'pytest', '-q', 'tests'],
        ):
            with (
                self.subTest(command=command),
                self.assertRaisesRegex(m.GateError, 'TDD_COMMAND_DENIED'),
            ):
                m.validate_tdd_command(self.root, command)
        # Preserve legitimate project-selected commands, including a venv interpreter.
        for command in (
            [sys.executable, '-m', 'pytest', '-q', 'tests'],
            [str(self.root / '.venv/bin/python'), '-m', 'pytest', '-q', 'tests'],
            ['uv', 'run', '--locked', 'pytest', '-q', 'tests'],
            ['pytest', '-q', 'tests'],
        ):
            m.validate_tdd_command(self.root, command)

    def test_tdd_records_feature_and_regression_checks_for_each_stage(self):
        m.approve(self.root)
        sample = self.root / 'tests/test_multi_scope.py'
        sample.write_text(
            'import unittest\n\nclass FeatureTest(unittest.TestCase):\n'
            '    def test_feature(self):\n        self.fail("feature absent")\n'
        )
        feature = [
            sys.executable,
            '-m',
            'unittest',
            'discover',
            '-s',
            'tests',
            '-p',
            'test_multi_scope.py',
        ]
        regression = [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests']
        m.record_tdd(self.root, 'multi', 'red', feature, 'new feature is absent', scope='feature')
        m.record_tdd(
            self.root,
            'multi',
            'red',
            regression,
            'full suite contains new failure',
            scope='regression',
        )
        sample.write_text(
            'import unittest\n\nclass FeatureTest(unittest.TestCase):\n'
            '    def test_feature(self):\n        self.assertTrue(True)\n'
        )
        m.record_tdd(self.root, 'multi', 'green', feature, scope='feature')
        m.record_tdd(self.root, 'multi', 'green', regression, scope='regression')
        m.record_tdd(self.root, 'multi', 'refactor', feature, scope='feature')
        m.record_tdd(self.root, 'multi', 'refactor', regression, scope='regression')
        entries = json.loads(m.tdd_path(self.root).read_text())['cycles']['multi']
        self.assertEqual(
            [(entry['stage'], entry['scope']) for entry in entries],
            [
                ('red', 'feature'),
                ('red', 'regression'),
                ('green', 'feature'),
                ('green', 'regression'),
                ('refactor', 'feature'),
                ('refactor', 'regression'),
            ],
        )
        self.assertTrue(
            all(
                'command' in entry and 'exit_code' in entry and 'fingerprint_before' in entry
                for entry in entries
            )
        )
        with self.assertRaisesRegex(m.GateError, 'TDD_SCOPE_DUPLICATE'):
            m.record_tdd(self.root, 'multi', 'refactor', regression, scope='regression')
        with self.assertRaisesRegex(m.GateError, 'TDD_ORDER'):
            m.record_tdd(self.root, 'skip', 'green', regression, scope='regression')
        with self.assertRaisesRegex(m.GateError, 'INVALID_TDD_SCOPE'):
            m.record_tdd(self.root, 'scope', 'red', feature, 'expected failure', scope='all')

    def test_baseline_runs_only_runner_selected_pytest_command(self):
        m.approve(self.root)
        with (
            patch.object(
                m, 'python_backend', return_value=(['/venv/bin/python', '-m'], ['/venv/bin/python'])
            ),
            patch.object(m, 'require_module'),
            patch.object(m, 'run') as run,
        ):
            m.baseline_test(self.root)
        run.assert_called_once_with(
            self.root,
            ['/venv/bin/python', '-m', 'pytest', '-q', 'tests'],
            capture=False,
            env=None,
            echo_capture=True,
        )

    def test_mutator_receives_pytest_backend_without_package_marker(self):
        self.assertFalse((self.root / 'tests/__init__.py').exists())
        command = m.mutator_command(self.root, ['/venv/bin/python', '-m'], ['example.py'])
        self.assertIn('--test-command', command)
        selected = command[command.index('--test-command') + 1]
        self.assertEqual(selected, '/venv/bin/python -m pytest -q tests')
        self.assertNotIn('unittest', selected)

    def test_mutator_command_keeps_locked_uv_backend(self):
        command = m.mutator_command(self.root, ['uv', 'run', '--locked'], ['src'])
        selected = command[command.index('--test-command') + 1]
        self.assertEqual(selected, 'uv run --locked pytest -q tests')

    def test_wheel_applicability_distinguishes_scripts_from_packages(self):
        self.assertEqual(
            m.wheel_applicability(self.root),
            {
                'applicable': False,
                'reason': 'no supported Python packaging metadata',
            },
        )

        (self.root / 'pyproject.toml').write_text('[tool.ruff]\nline-length = 100\n')
        self.assertEqual(
            m.wheel_applicability(self.root),
            {
                'applicable': False,
                'reason': 'pyproject.toml contains tool configuration only',
            },
        )

        (self.root / 'pyproject.toml').write_text(
            '[build-system]\nrequires=[]\nbuild-backend="example.backend"\n'
        )
        self.assertEqual(
            m.wheel_applicability(self.root),
            {
                'applicable': True,
                'reason': 'pyproject.toml declares build-system',
            },
        )

    def test_wheel_applicability_supports_project_and_legacy_metadata(self):
        (self.root / 'pyproject.toml').write_text('[project]\nname="example"\nversion="1.0"\n')
        self.assertTrue(m.wheel_applicability(self.root)['applicable'])
        (self.root / 'pyproject.toml').unlink()

        (self.root / 'setup.py').write_text('from setuptools import setup\nsetup()\n')
        self.assertTrue(m.wheel_applicability(self.root)['applicable'])
        (self.root / 'setup.py').unlink()

        (self.root / 'setup.cfg').write_text('[metadata]\nname = example\nversion = 1.0\n')
        self.assertTrue(m.wheel_applicability(self.root)['applicable'])

    def test_tool_only_pyproject_does_not_hide_legacy_packaging(self):
        (self.root / 'pyproject.toml').write_text('[tool.ruff]\nline-length = 100\n')
        (self.root / 'setup.py').write_text('from setuptools import setup\nsetup()\n')

        result = m.wheel_applicability(self.root)

        self.assertTrue(result['applicable'])
        self.assertEqual(result['reason'], 'legacy setup.py packaging metadata')

    def test_verify_skips_wheel_for_script_only_project(self):
        m.approve(self.root)
        with (
            patch.object(m, 'require_module'),
            patch('runner.agentic.quality.generate_python_coverage'),
            patch.object(m, 'tool_path', side_effect=lambda name: Path('/tools') / name),
            patch.object(m, 'run', return_value=''),
            patch.object(m, 'wheel_check') as wheel,
        ):
            result = m.quality(self.root, 'verify')

        wheel.assert_not_called()
        self.assertEqual(result['status'], 'not_applicable')
        self.assertFalse(result['applicable'])

    def test_verify_requires_wheel_for_packaging_project(self):
        m.approve(self.root)
        (self.root / 'pyproject.toml').write_text(
            '[build-system]\nrequires=[]\nbuild-backend="example.backend"\n'
        )
        with (
            patch.object(m, 'require_module'),
            patch('runner.agentic.quality.generate_python_coverage'),
            patch.object(m, 'tool_path', side_effect=lambda name: Path('/tools') / name),
            patch.object(m, 'run', return_value=''),
            patch.object(
                m,
                'wheel_check',
                return_value={
                    'applicable': True,
                    'status': 'verified',
                    'reason': 'wheel build, install and isolated smoke passed',
                },
            ) as wheel,
        ):
            result = m.quality(self.root, 'verify')

        wheel.assert_called_once()
        self.assertEqual(wheel.call_args.args, (self.root, [sys.executable]))
        self.assertIsInstance(wheel.call_args.kwargs['_journal'], m.CommandJournal)
        self.assertEqual(result['status'], 'verified')

    def test_receipts_distinguish_verified_not_applicable_and_failed_wheel(self):
        m.approve(self.root)

        with patch.object(
            m,
            'quality',
            return_value={
                'applicable': False,
                'status': 'not_applicable',
                'reason': 'no supported Python packaging metadata',
            },
        ):
            m.gate(self.root, 'verify')
        not_applicable = json.loads(m.receipt_path(self.root, 'verify').read_text())
        self.assertFalse(not_applicable['wheel_verified'])
        self.assertEqual(not_applicable['wheel']['status'], 'not_applicable')

        with patch.object(
            m,
            'quality',
            return_value={
                'applicable': True,
                'status': 'verified',
                'reason': 'wheel build, install and isolated smoke passed',
            },
        ):
            m.gate(self.root, 'verify')
        verified = json.loads(m.receipt_path(self.root, 'verify').read_text())
        self.assertTrue(verified['wheel_verified'])
        self.assertEqual(verified['wheel']['status'], 'verified')

        (self.root / 'pyproject.toml').write_text(
            '[build-system]\nrequires=[]\nbuild-backend="broken.backend"\n'
        )

        def broken_quality(project, _level, _runner=None):
            return m.wheel_check(project, [sys.executable])

        with (
            patch.object(m, 'quality', side_effect=broken_quality),
            patch.object(
                m, 'run', side_effect=m.GateFailure('COMMAND_FAILED: packaging build failed')
            ),
            self.assertRaises(m.GateFailure),
        ):
            m.gate(self.root, 'verify')
        failed = json.loads(m.receipt_path(self.root, 'verify').read_text())
        self.assertFalse(failed['passed'])
        self.assertFalse(failed['wheel_verified'])
        self.assertEqual(failed['wheel']['status'], 'failed')
        with self.assertRaisesRegex(m.GateError, 'FAILED_VERIFY_RECEIPT'):
            m.check_receipt(self.root, 'verify')

    def test_fast_receipt_keeps_wheel_unverified(self):
        m.approve(self.root)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'fast')
        receipt = json.loads(m.receipt_path(self.root, 'fast').read_text())
        self.assertFalse(receipt['wheel_verified'])
        self.assertEqual(receipt['wheel']['status'], 'not_run')

    def test_wheel_check_builds_installs_and_tests_outside_project(self):
        (self.root / 'pyproject.toml').write_text(
            '[build-system]\nrequires=[]\nbuild-backend="x"\n'
        )
        seen = []

        def fake_run(cwd, argv, **kwargs):
            seen.append((Path(cwd), argv, kwargs))
            if 'wheel' in argv and '--wheel-dir' in argv:
                dist = Path(argv[argv.index('--wheel-dir') + 1])
                with zipfile.ZipFile(dist / 'example-1-py3-none-any.whl', 'w') as wheel:
                    wheel.writestr('example.py', 'def answer(): return 42\n')
                    wheel.writestr('example-1.dist-info/top_level.txt', 'example\n')
            if 'sysconfig.get_paths' in ' '.join(argv):
                return json.dumps({'purelib': str(Path(sys.executable).parent.parent / 'lib')})
            return ''

        with patch.object(m, 'run', side_effect=fake_run):
            m.wheel_check(self.root, [sys.executable])
        self.assertEqual(len(seen), 5)
        self.assertTrue(all(cwd != self.root for cwd, _, _ in seen))
        self.assertIn('-I', seen[-1][1])
        self.assertIn('-S', seen[-1][1])
        self.assertEqual(seen[-1][2]['env']['PYTHONNOUSERSITE'], '1')
        self.assertNotIn('PYTHONPATH', seen[-1][2]['env'])

    def test_uv_wheel_check_is_offline_and_locked(self):
        (self.root / 'pyproject.toml').write_text(
            '[build-system]\nrequires=[]\nbuild-backend="x"\n'
        )
        seen = []

        def fake_run(cwd, argv, **kwargs):
            seen.append(argv)
            if '--wheel' in argv and '--out-dir' in argv:
                dist = Path(argv[argv.index('--out-dir') + 1])
                with zipfile.ZipFile(dist / 'example-1-py3-none-any.whl', 'w') as wheel:
                    wheel.writestr('example.py', 'def answer(): return 42\n')
                    wheel.writestr('example-1.dist-info/top_level.txt', 'example\n')
            if 'sysconfig.get_paths' in ' '.join(argv):
                return json.dumps({'purelib': '/tools/site-packages'})
            return ''

        with patch.object(m, 'run', side_effect=fake_run):
            m.wheel_check(self.root, ['uv', 'run', '--locked'])
        self.assertIn('--no-build-isolation', seen[0])
        self.assertIn('--offline', seen[0])
        self.assertIn('--no-python-downloads', seen[0])
        self.assertIn('--offline', seen[1])
        self.assertEqual(
            seen[2][:6], ['uv', 'run', '--project', str(self.root), '--locked', 'python']
        )

    def test_isolated_wheel_smoke_rejects_wrong_artifact_despite_working_source(self):
        (self.root / 'pyproject.toml').write_text(
            '[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n'
        )
        (self.root / 'tests/__init__.py').write_text('')
        (self.root / 'tests/test_example.py').write_text(
            'import unittest\nfrom example import answer\n\n'
            'class ExampleTest(unittest.TestCase):\n'
            '    def test_answer(self):\n        self.assertEqual(answer(), 42)\n'
        )
        # The checkout works, while the simulated installed wheel target is wrong.
        local = subprocess.run(
            [sys.executable, '-m', 'unittest', 'discover', '-s', 'tests'],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(local.returncode, 0, local.stdout + local.stderr)
        scratch = Path(self.temp.name) / 'isolated-smoke'
        scratch.mkdir()
        site = scratch / 'site'
        site.mkdir()
        (site / 'example.py').write_text('def answer():\n    return 0\n')
        # Minimal test-runner stand-in keeps this regression independent of host pytest.
        (site / 'pytest.py').write_text(
            'import unittest\n\ndef main(args):\n'
            '    suite = unittest.defaultTestLoader.discover(args[1])\n'
            '    return 0 if unittest.TextTestRunner().run(suite).wasSuccessful() else 1\n'
        )
        wheel = scratch / 'example-1-py3-none-any.whl'
        with zipfile.ZipFile(wheel, 'w') as archive:
            archive.writestr('example.py', 'def answer():\n    return 0\n')
            archive.writestr('example-1.dist-info/top_level.txt', 'example\n')

        command_log = []
        verifier = WheelVerifier(m.run, command_log, m._python_invocation, m.sha)
        with self.assertRaisesRegex(m.GateFailure, 'COMMAND_FAILED'):
            verifier.verify_installed_artifact(self.root, [sys.executable], scratch, site, wheel)

    def test_fast_commands_in_flat_layout(self):
        m.approve(self.root)
        seen = []
        with (
            patch.object(m, 'require_module'),
            patch.object(m, 'run', side_effect=lambda p, argv, **kw: seen.append(argv)),
        ):
            m.quality(self.root, 'fast')
        self.assertEqual(len(seen), 8)
        self.assertTrue(any('--version' in argv for argv in seen))
        self.assertTrue(any('example.py' in argv for argv in seen))
        self.assertTrue(any('tests/test_example.py' in argv for argv in seen))
        self.assertTrue(any('pytest' in argv for argv in seen))

    def test_full_gate_passes_nested_script_to_all_production_python_checks(self):
        m.approve(self.root)
        (self.root / 'runner').mkdir()
        (self.root / 'runner/agentic_gate.py').write_text('VALUE = 1\n')
        seen = []
        coverage_sources = []

        def capture_coverage(_project, _backend, _python, sources, report, **_kwargs):
            coverage_sources.extend(sources)
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text('fresh')

        with (
            patch.object(m, 'require_module'),
            patch(
                'runner.agentic.quality.generate_python_coverage',
                side_effect=capture_coverage,
            ),
            patch.object(m, 'tool_path', side_effect=lambda name: Path('/tools') / name),
            patch.object(m, 'run', side_effect=lambda _p, argv, **_kw: seen.append(argv) or ''),
            patch.object(
                m,
                'wheel_applicability',
                return_value={'applicable': False, 'reason': 'no packaging metadata'},
            ),
        ):
            m.quality(self.root, 'full')

        nested = 'runner/agentic_gate.py'
        ruff = [argv for argv in seen if 'ruff' in argv and '--version' not in argv]
        mypy = [argv for argv in seen if 'mypy' in argv and '--version' not in argv]
        crapper = [argv for argv in seen if argv and argv[0].endswith('/crapper')]
        dryer = [argv for argv in seen if argv and argv[0].endswith('/dryer')]
        mutator = [argv for argv in seen if argv and argv[0].endswith('/mutator')]
        self.assertTrue(ruff and all(nested in argv for argv in ruff))
        self.assertTrue(mypy and all(nested in argv for argv in mypy))
        self.assertTrue(crapper and nested in crapper[0])
        self.assertTrue(dryer and nested in dryer[0])
        self.assertTrue(mutator and nested in mutator[0])
        self.assertIn(nested, coverage_sources)
        self.assertTrue(all('tests' not in argv for argv in [*mypy, *crapper, *dryer, *mutator]))

    def test_coverage_expands_discovered_nested_script_without_tests(self):
        (self.root / 'runner').mkdir()
        (self.root / 'runner/agentic_gate.py').write_text('VALUE = 1\n')
        sources, _lint = m.source_targets(self.root)

        files = coverage_source_files(self.root, sources)

        self.assertIn(Path('runner/agentic_gate.py'), files)
        self.assertNotIn(Path('tests/test_example.py'), files)

    def _coverage_fixture(self, source: str, test: str, source_path: str = 'example.py') -> Path:
        fixture_number = len(list(Path(self.temp.name).iterdir()))
        root = Path(self.temp.name) / f'coverage-{fixture_number}'
        root.mkdir()
        path = root / source_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
        (root / 'tests').mkdir()
        (root / 'tests/test_coverage.py').write_text(test)
        return root

    def _assert_real_coverage(self, root: Path, sources: list[str]) -> str:
        if importlib.util.find_spec('pytest_cov') is None:
            self.skipTest('pytest-cov is not installed in the test interpreter')
        backend = [sys.executable, '-m']
        report = root / 'target/coverage/python/lcov.info'
        m.python_coverage(root, backend, [sys.executable], sources, report)
        text = report.read_text()
        self.assertIn('DA:', text)
        self.assertRegex(text, r'DA:\d+,[1-9]\d*')
        return text

    def test_coverage_flat_layout_with_regular_import(self):
        root = self._coverage_fixture(
            'def answer():\n    return 42\n',
            'from example import answer\n\ndef test_answer():\n    assert answer() == 42\n',
        )

        lcov = self._assert_real_coverage(root, ['example.py'])

        self.assertIn('SF:example.py', lcov)

    def test_coverage_flat_layout_with_spec_from_file_location(self):
        root = self._coverage_fixture(
            'def answer():\n    return 42\n',
            'import importlib.util\nfrom pathlib import Path\n\n'
            'path = Path(__file__).parents[1] / "example.py"\n'
            'spec = importlib.util.spec_from_file_location(\n'
            '    "installer_under_test", path)\n'
            'assert spec and spec.loader\n'
            'module = importlib.util.module_from_spec(spec)\n'
            'spec.loader.exec_module(module)\n\n'
            'def test_answer():\n    assert module.answer() == 42\n',
        )

        lcov = self._assert_real_coverage(root, ['example.py'])

        self.assertIn('SF:example.py', lcov)

    def test_coverage_regular_package(self):
        root = self._coverage_fixture(
            'def answer():\n    return 42\n',
            'from sample import answer\n\ndef test_answer():\n    assert answer() == 42\n',
            'sample/__init__.py',
        )

        lcov = self._assert_real_coverage(root, ['sample'])

        self.assertIn('SF:sample/__init__.py', lcov)

    def test_coverage_records_production_code_executed_only_in_subprocess(self):
        root = self._coverage_fixture(
            'def main():\n    print("subprocess covered")\n\n'
            'if __name__ == "__main__":\n    main()\n',
            'import subprocess\nimport sys\nfrom pathlib import Path\n\n'
            'def test_worker_subprocess():\n'
            '    worker = Path(__file__).parents[1] / "worker.py"\n'
            '    done = subprocess.run([sys.executable, str(worker)], check=False)\n'
            '    assert done.returncode == 0\n',
            'worker.py',
        )

        lcov = self._assert_real_coverage(root, ['worker.py'])
        record = next(part for part in lcov.split('end_of_record') if 'SF:worker.py' in part)

        self.assertRegex(record, r'DA:\d+,[1-9]\d*')

    def test_missing_new_coverage_removes_stale_lcov_and_fails(self):
        report = self.root / 'target/coverage/python/lcov.info'
        report.parent.mkdir(parents=True)
        report.write_text('stale')

        with (
            patch.object(m, 'run'),
            self.assertRaisesRegex(m.GateError, 'MISSING_COVERAGE_ARTIFACT'),
        ):
            m.python_coverage(self.root, ['python', '-m'], ['python'], ['example.py'], report)

        self.assertFalse(report.exists())

    def test_valid_new_coverage_atomically_replaces_stale_lcov(self):
        report = self.root / 'target/coverage/python/lcov.info'
        report.parent.mkdir(parents=True)
        report.write_text('stale')
        fresh = 'SF:example.py\nDA:1,1\nLF:1\nLH:1\nend_of_record\n'

        def fake_run(_project, command, **_kwargs):
            if 'lcov' in command:
                Path(command[command.index('-o') + 1]).write_text(fresh)
            return ''

        with patch.object(m, 'run', side_effect=fake_run):
            m.python_coverage(self.root, ['python', '-m'], ['python'], ['example.py'], report)

        self.assertEqual(report.read_text(), fresh)

    def test_coverage_rejects_report_without_data_or_with_unowned_source(self):
        report = self.root / 'lcov.info'
        for content in (
            'SF:example.py\nLF:0\nLH:0\nend_of_record\n',
            'SF:tests/test_example.py\nDA:1,1\nLF:1\nLH:1\nend_of_record\n',
        ):
            report.write_text(content)
            with self.assertRaisesRegex(m.GateError, 'INVALID_COVERAGE_ARTIFACT'):
                validate_lcov(self.root, report, ['example.py'])

    def test_coverage_rejects_partial_or_malformed_line_data(self):
        report = self.root / 'lcov.info'
        (self.root / 'other.py').write_text('value = 1\n')
        for content in (
            'SF:example.py\nDA:not-data\nLF:1\nLH:1\nend_of_record\n',
            'SF:example.py\nDA:1,1\nLF:1\nLH:1\nend_of_record\n',
        ):
            report.write_text(content)
            with self.assertRaisesRegex(m.GateError, 'INVALID_COVERAGE_ARTIFACT'):
                validate_lcov(self.root, report, ['example.py', 'other.py'])

    def test_crapper_runs_only_after_fresh_coverage_is_published(self):
        m.approve(self.root)
        report = self.root / 'target/coverage/python/lcov.info'
        observations = []

        def publish(_project, _backend, _python, _sources, destination, **_kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text('fresh')

        def fake_run(_project, command, **_kwargs):
            if command and command[0].endswith('/crapper'):
                observations.append(report.read_text())
            return ''

        with (
            patch.object(m, 'require_module'),
            patch('runner.agentic.quality.generate_python_coverage', side_effect=publish),
            patch.object(m, 'tool_path', side_effect=lambda name: Path('/tools') / name),
            patch.object(m, 'wheel_check'),
            patch.object(m, 'run', side_effect=fake_run),
        ):
            m.quality(self.root, 'verify')

        self.assertEqual(observations, ['fresh'])

    def test_coverage_failure_aborts_gate_before_crapper(self):
        m.approve(self.root)
        seen = []
        with (
            patch.object(m, 'require_module'),
            patch(
                'runner.agentic.quality.generate_python_coverage',
                side_effect=m.GateError('MISSING_COVERAGE_ARTIFACT'),
            ),
            patch.object(m, 'tool_path', side_effect=lambda name: Path('/tools') / name),
            patch.object(m, 'wheel_check'),
            patch.object(m, 'run', side_effect=lambda _p, argv, **_kw: seen.append(argv)),
            self.assertRaisesRegex(m.GateError, 'MISSING_COVERAGE_ARTIFACT'),
        ):
            m.quality(self.root, 'verify')

        self.assertFalse(any(argv and argv[0].endswith('/crapper') for argv in seen))

    def test_no_lock_no_install(self):
        backend, python = m.python_backend(self.root)
        self.assertEqual(backend[1:], ['-m'])
        self.assertTrue(python[0].startswith('/'))
        self.assertFalse((self.root / 'uv.lock').exists())

    def test_cli_non_repo_blocked(self):
        result = subprocess.run(
            ['python3', str(SRC), '--project', self.temp.name, 'check-approval'],
            capture_output=True,
            text=True,
            check=False,
        )
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
        self.assertTrue(a_drafts[0].parent.is_dir())
        a_drafts[0].write_text('Task A spec')
        a_drafts[1].write_text('Task A plan')
        m.approve(self.root)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'full')
        a_receipt = m.receipt_path(self.root, 'full')
        a_baseline = m.approved_paths(self.root)
        self.assertEqual(json.loads(a_receipt.read_text())['task_id'], 'task-a')
        review = m.begin_review(self.root)
        m.submit_review(self.root, review['review_id'], 'pass')
        m.accept_review(self.root, review['review_id'])

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
        self.assertTrue(b_drafts[0].parent.is_dir())
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

    def test_finalize_review_requires_separate_current_human_acceptance(self):
        m.approve(self.root)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'verify')
        source = m.begin_review(self.root, 'verify')
        m.submit_review(self.root, source['review_id'], 'pass', [])
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'full')
        review = m.begin_review(self.root, 'finalize')
        duplicate = m.begin_review(self.root, 'finalize')
        self.assertTrue(review['created'])
        self.assertEqual(review['mode'], 'reuse-independent-review')
        self.assertEqual(review['source_review_id'], source['review_id'])
        self.assertFalse(duplicate['created'])
        self.assertEqual(review['review_id'], duplicate['review_id'])
        self.assertTrue(m.review_status(self.root)['current'])
        registry = m.load_registry(self.root)
        self.assertIsNone(registry['tasks']['legacy']['completed_at'])
        with self.assertRaisesRegex(m.GateError, 'REVIEW_NOT_PASSED'):
            m.accept_review(self.root, review['review_id'])

        m.submit_review(self.root, review['review_id'], 'pass', [])
        accepted = m.accept_review(self.root, review['review_id'])
        self.assertEqual(accepted['task_id'], 'legacy')
        self.assertEqual(accepted['review_id'], review['review_id'])
        self.assertIn('full_receipt_sha256', accepted)
        self.assertEqual(m.accept_review(self.root, review['review_id'])['already_completed'], True)
        with self.assertRaisesRegex(m.GateError, 'DIRECT_COMPLETE_DISABLED'):
            m.complete_task(self.root)

    def test_accept_without_id_uses_only_current_final_review_pointer(self):
        m.approve(self.root)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'full')
        review = m.begin_review(self.root, 'finalize')
        m.submit_review(self.root, review['review_id'], 'pass', [])

        accepted = m.accept_review(self.root, None)

        self.assertEqual(accepted['review_id'], review['review_id'])
        self.assertTrue(m.accept_review(self.root, None)['already_completed'])

    def test_accept_without_id_fails_closed_when_current_pointer_is_missing(self):
        m.approve(self.root)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'full')
        review = m.begin_review(self.root, 'finalize')
        m.submit_review(self.root, review['review_id'], 'pass', [])
        (m.task_state_dir(self.root) / 'review-current.json').unlink()

        with self.assertRaisesRegex(m.GateError, 'CURRENT_FINAL_REVIEW_REQUIRED'):
            m.accept_review(self.root, None)

    def test_rejected_exception_and_stale_reviews_never_complete(self):
        m.approve(self.root)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'full')

        rejected = m.begin_review(self.root, 'finalize')
        m.submit_review(
            self.root, rejected['review_id'], 'reject', ['major:example.py:1:incorrect-result']
        )
        with self.assertRaisesRegex(m.GateError, 'REVIEW_NOT_PASSED'):
            m.accept_review(self.root, rejected['review_id'])

        exception = m.begin_review(self.root, 'finalize')
        result = m.submit_review(
            self.root, exception['review_id'], 'exception', ['process:runner-timeout']
        )
        self.assertFalse(result['checks_completed'])
        self.assertIn('process_exception', result)
        with self.assertRaisesRegex(m.GateError, 'REVIEW_NOT_PASSED'):
            m.accept_review(self.root, exception['review_id'])

        stale = m.begin_review(self.root, 'finalize')
        receipt = m.receipt_path(self.root, 'full')
        data = json.loads(receipt.read_text())
        data['passed_at'] = '2099-01-01T00:00:00+00:00'
        receipt.write_text(json.dumps(data))
        with self.assertRaisesRegex(m.GateError, 'STALE_REVIEW'):
            m.submit_review(self.root, stale['review_id'], 'pass', [])
        record = json.loads(m.review_path(self.root, stale['review_id']).read_text())
        self.assertEqual(record['status'], 'stale')
        self.assertIsNone(m.load_registry(self.root)['tasks']['legacy']['completed_at'])

    def test_audited_reopen_preserves_premature_completion(self):
        m.approve(self.root)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'full')
        full_before = m.receipt_path(self.root, 'full').read_bytes()
        registry = m.load_registry(self.root)
        old = '2026-11-13T00:50:22+00:00'
        registry['tasks']['legacy']['completed_at'] = old
        m._write_json_atomic(m.registry_path(self.root), registry)

        result = m.reopen_task(self.root, 'legacy')

        self.assertEqual(result['previous_completed_at'], old)
        task = m.load_registry(self.root)['tasks']['legacy']
        self.assertIsNone(task['completed_at'])
        self.assertEqual(task['status_history'][-1]['previous_completed_at'], old)
        self.assertEqual(
            task['status_history'][-1]['reason'], 'premature-finalize-without-human-acceptance'
        )
        archive = Path(result['archive'])
        self.assertEqual((archive / 'full.json').read_bytes(), full_before)
        manifest = json.loads((archive / 'reopen.json').read_text())
        self.assertEqual(manifest['task_before']['completed_at'], old)

    def test_reopen_distinguishes_prior_human_acceptance_in_audit(self):
        m.approve(self.root)
        registry = m.load_registry(self.root)
        task = registry['tasks']['legacy']
        task['completed_at'] = '2026-11-13T00:50:22+00:00'
        task['acceptance'] = {'review_id': 'a' * 32, 'accepted_at': task['completed_at']}
        m._write_json_atomic(m.registry_path(self.root), registry)

        result = m.reopen_task(self.root, 'legacy')

        history = m.load_registry(self.root)['tasks']['legacy']['status_history'][-1]
        self.assertEqual(history['reason'], 'human-reopened-after-acceptance')
        self.assertEqual(history['previous_acceptance']['review_id'], 'a' * 32)
        manifest = json.loads((Path(result['archive']) / 'reopen.json').read_text())
        self.assertEqual(manifest['reason'], 'human-reopened-after-acceptance')

    def test_verify_review_is_persisted_and_reused_only_while_current(self):
        m.approve(self.root)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'verify')
        source = m.begin_review(self.root, 'verify')
        submitted = m.submit_review(
            self.root, source['review_id'], 'pass', ['minor:example.py:1:naming-note']
        )
        self.assertEqual(submitted['phase'], 'verify')
        self.assertEqual(submitted['binding']['task_id'], 'legacy')
        self.assertIn('verify_receipt_sha256', submitted['binding'])
        self.assertEqual(submitted['findings'][0]['severity'], 'minor')

        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'full')
        final = m.begin_review(self.root, 'finalize')
        self.assertEqual(final['mode'], 'reuse-independent-review')
        self.assertEqual(final['source_review_id'], source['review_id'])
        self.assertEqual(final['carried_findings'], submitted['findings'])

        (self.root / 'example.py').write_text('changed = True\n')
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'full')
        fresh = m.begin_review(self.root, 'finalize')
        self.assertEqual(fresh['mode'], 'full-independent-review')
        self.assertIsNone(fresh['source_review_id'])
        invalidated = json.loads(m.review_path(self.root, source['review_id']).read_text())
        self.assertEqual(invalidated['status'], 'stale')
        self.assertEqual(invalidated['previous_status'], 'passed')

    def test_unresolved_major_findings_and_nonfinal_reviews_block_acceptance(self):
        m.approve(self.root)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'verify')
        source = m.begin_review(self.root, 'verify')
        m.submit_review(
            self.root, source['review_id'], 'reject', ['major:example.py:1:incorrect-result']
        )
        with self.assertRaisesRegex(m.GateError, 'REVIEW_NOT_FINAL'):
            m.accept_review(self.root, source['review_id'])

        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'full')
        final = m.begin_review(self.root, 'finalize')
        self.assertEqual(final['mode'], 'reuse-independent-review')
        with self.assertRaisesRegex(m.GateError, 'UNRESOLVED_MAJOR_FINDINGS'):
            m.submit_review(self.root, final['review_id'], 'pass', [])

    def test_unresolved_process_findings_from_verify_block_finalize_pass(self):
        """A rejected VERIFY process finding must not disappear in FINALIZE."""
        m.approve(self.root)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'verify')
        prior = m.begin_review(self.root, 'verify')
        m.submit_review(
            self.root, prior['review_id'], 'reject', ['process:tests:missing-regression-evidence']
        )

        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'full')
        final = m.begin_review(self.root, 'finalize')
        self.assertEqual(final['mode'], 'reuse-independent-review')
        self.assertEqual(
            final['carried_findings'],
            [
                {'severity': 'process', 'detail': 'tests:missing-regression-evidence'},
            ],
        )
        with self.assertRaisesRegex(m.GateError, 'UNRESOLVED_PROCESS_FINDINGS'):
            m.submit_review(self.root, final['review_id'], 'pass', [])
        with self.assertRaisesRegex(m.GateError, 'REVIEW_NOT_PASSED'):
            m.accept_review(self.root, final['review_id'])

    def test_reapproval_invalidates_old_independent_review(self):
        m.approve(self.root)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'verify')
        source = m.begin_review(self.root, 'verify')
        m.submit_review(self.root, source['review_id'], 'pass', [])
        spec, plan = m.draft_paths(self.root)
        spec.write_text('# Revised spec\n\n**Статус:** DRAFT\n\nNew approved requirement.\n')
        plan.write_text('# Revised plan\n\n**Статус:** DRAFT\n\nNew approved steps.\n')
        m.approve(self.root, replace_existing=True)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'full')

        final = m.begin_review(self.root, 'finalize')

        self.assertEqual(final['mode'], 'full-independent-review')
        old = json.loads(m.review_path(self.root, source['review_id']).read_text())
        self.assertEqual(old['status'], 'stale')
        self.assertIn('approval', old['invalidation_reason'])

    def test_reopen_archive_preserves_review_provenance_and_tdd(self):
        m.task_new(self.root, 'task-b')
        spec, plan = m.draft_paths(self.root)
        spec.write_text('Task B spec')
        plan.write_text('Task B plan')
        m.approve(self.root)
        m.tdd_path(self.root).write_text(json.dumps({'cycles': {'historical': []}}))
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'verify')
        source = m.begin_review(self.root, 'verify')
        m.submit_review(self.root, source['review_id'], 'pass', [])
        registry = m.load_registry(self.root)
        registry['tasks']['task-b']['completed_at'] = '2026-11-13T00:50:22+00:00'
        m._write_json_atomic(m.registry_path(self.root), registry)

        result = m.reopen_task(self.root, 'task-b')
        archive = Path(result['archive'])
        self.assertTrue((archive / 'tdd.json').is_file())
        archived = json.loads((archive / 'reviews' / f'{source["review_id"]}.json').read_text())
        self.assertEqual(archived['binding']['task_id'], 'task-b')
        context = json.loads((archive / 'reopen.json').read_text())
        self.assertIn('reviews/', context['evidence_files'])
        self.assertTrue((archive / 'review-verify-current.json').is_file())
        self.assertTrue(m.tdd_path(self.root).is_file(), 'historical TDD stays available in place')
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'full')
        final = m.begin_review(self.root, 'finalize')
        self.assertEqual(final['mode'], 'full-independent-review')
        output = io.StringIO()
        with redirect_stdout(output):
            m.review_context(self.root, 'full')
        reviewer_context = json.loads(output.getvalue())
        self.assertTrue(reviewer_context['task_audit']['reopen_archives'])

    def test_superseded_pending_review_cannot_be_accepted(self):
        m.approve(self.root)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'full')
        first = m.begin_review(self.root, 'finalize')
        (self.root / 'example.py').write_text('changed = True\n')
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'full')
        second = m.begin_review(self.root, 'finalize')
        self.assertNotEqual(first['review_id'], second['review_id'])
        prior = json.loads(m.review_path(self.root, first['review_id']).read_text())
        self.assertEqual(prior['status'], 'superseded')
        with self.assertRaisesRegex(m.GateError, 'REVIEW_NOT_PASSED'):
            m.accept_review(self.root, first['review_id'])

    def test_legacy_state_is_adopted_without_moving_history(self):
        m.approve(self.root)
        legacy_baseline = m.state_dir(self.root) / 'baseline/spec.md'
        self.assertTrue(legacy_baseline.is_file())
        registry = m.load_registry(self.root)
        self.assertEqual(registry['active_task'], 'legacy')
        self.assertEqual(registry['tasks']['legacy']['layout'], 'legacy')
        self.assertEqual(m.approved_paths(self.root)[0], legacy_baseline)
        with patch.object(m, 'quality', side_effect=passing_quality):
            m.gate(self.root, 'full')
        receipt = json.loads(m.receipt_path(self.root, 'full').read_text())
        receipt.pop('task_id')
        receipt['baseline'].pop('task_id')
        m.receipt_path(self.root, 'full').write_text(json.dumps(receipt))
        m.check_receipt(self.root, 'full')


if __name__ == '__main__':
    unittest.main()
