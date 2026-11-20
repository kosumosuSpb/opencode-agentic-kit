from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner.agentic import approval as approval_module
from runner.agentic.approval import APPROVED_STATUS, ApprovalService
from runner.agentic.errors import GateError
from runner.agentic.storage import JsonStorage
from runner.agentic.tasks import TaskService
from runner.agentic.tdd import TDDRecorder


class ApprovalTDDRotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        self.state = Path(self.temp.name) / 'state'
        environment = patch.dict(os.environ, {'AGENTIC_STATE_DIR': str(self.state)})
        environment.start()
        self.addCleanup(environment.stop)
        self.storage = JsonStorage()
        self.tasks = TaskService(self.project, self.storage)
        self.tasks.create('task-a')
        self.approval = ApprovalService(self.project, self.tasks, self.storage)
        self._write_drafts('A')
        self.approval.approve()

    def _write_drafts(self, revision: str) -> None:
        spec, plan = self.tasks.draft_paths()
        spec.write_text(f'# Spec {revision}\n\nRequirement {revision}.\n')
        plan.write_text(f'# Plan {revision}\n\nSteps {revision}.\n')

    def _write_tdd(self, cycle: str = 'old') -> bytes:
        document = {
            'baseline': self.approval.validate_baseline(),
            'cycles': {
                cycle: [
                    {
                        'stage': 'red',
                        'scope': 'feature',
                        'expected_failure': f'{cycle} failure',
                        'proof': {'nested': ['must', 'remain', 'exact']},
                    }
                ]
            },
        }
        self.storage.write(self.tasks.state_path() / 'tdd.json', document)
        return (self.tasks.state_path() / 'tdd.json').read_bytes()

    def _approval_bytes(self) -> dict[Path, bytes]:
        paths = (*self.tasks.working_paths(), *self.tasks.baseline_paths())
        return {path: path.read_bytes() for path in paths}

    def test_baseline_change_outside_replace_remains_rejected(self):
        self._write_tdd()
        replacement = f'{APPROVED_STATUS}\nchanged\n'.encode()
        for path in (*self.tasks.working_paths(), *self.tasks.baseline_paths()):
            path.write_bytes(replacement)
        recorder = TDDRecorder(
            self.project,
            self.tasks,
            self.approval,
            self.storage,
            process_runner=lambda *_args, **_kwargs: self.fail('command must not run'),
        )

        with self.assertRaisesRegex(GateError, 'TDD_BASELINE_CHANGED'):
            recorder.record(
                'new',
                'red',
                ['python3', '-m', 'unittest'],
                'new failure',
            )

    def test_archive_failure_preserves_approval_and_active_tdd(self):
        old_approval = self._approval_bytes()
        old_tdd = self._write_tdd()
        self._write_drafts('B')

        with (
            patch.object(self.approval, 'archive', side_effect=OSError('archive failed')),
            self.assertRaisesRegex(OSError, 'archive failed'),
        ):
            self.approval.approve(replace_existing=True)

        self.assertEqual(self._approval_bytes(), old_approval)
        self.assertEqual((self.tasks.state_path() / 'tdd.json').read_bytes(), old_tdd)

    def test_document_replacement_failure_rolls_back_without_rotating_tdd(self):
        old_approval = self._approval_bytes()
        old_tdd = self._write_tdd()
        self._write_drafts('B')
        failed = False
        original_replace = approval_module.os.replace

        def interrupted_replace(source: Path, destination: Path) -> None:
            nonlocal failed
            if destination == self.tasks.working_paths()[0] and not failed:
                failed = True
                raise OSError('document replacement failed')
            original_replace(source, destination)

        with (
            patch.object(approval_module.os, 'replace', side_effect=interrupted_replace),
            self.assertRaisesRegex(OSError, 'document replacement failed'),
        ):
            self.approval.approve(replace_existing=True)

        self.assertEqual(self._approval_bytes(), old_approval)
        self.assertEqual((self.tasks.state_path() / 'tdd.json').read_bytes(), old_tdd)
        self.assertEqual(list((self.tasks.state_path() / 'approval-history').iterdir()), [])

    def test_rotation_failure_rolls_back_documents_and_preserves_active_tdd(self):
        old_approval = self._approval_bytes()
        old_tdd = self._write_tdd()
        self._write_drafts('B')
        tdd_path = self.tasks.state_path() / 'tdd.json'
        original_replace = approval_module.os.replace

        def interrupted_replace(source: Path, destination: Path) -> None:
            if source == tdd_path:
                raise OSError('rotation failed')
            original_replace(source, destination)

        with (
            patch.object(approval_module.os, 'replace', side_effect=interrupted_replace),
            self.assertRaisesRegex(OSError, 'rotation failed'),
        ):
            self.approval.approve(replace_existing=True)

        self.assertEqual(self._approval_bytes(), old_approval)
        self.assertEqual(tdd_path.read_bytes(), old_tdd)
        self.assertEqual(list((self.tasks.state_path() / 'approval-history').iterdir()), [])

    def test_final_validation_failure_restores_rotated_tdd_and_old_approval(self):
        old_approval = self._approval_bytes()
        old_tdd = self._write_tdd()
        self._write_drafts('B')
        original_validate = self.approval._validate_target_baseline
        calls = 0

        def interrupted_validate(target, require_approved_status: bool) -> dict[str, str]:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError('final validation failed')
            return original_validate(target, require_approved_status)

        with (
            patch.object(
                self.approval,
                '_validate_target_baseline',
                side_effect=interrupted_validate,
            ),
            self.assertRaisesRegex(OSError, 'final validation failed'),
        ):
            self.approval.approve(replace_existing=True)

        self.assertEqual(self._approval_bytes(), old_approval)
        self.assertEqual((self.tasks.state_path() / 'tdd.json').read_bytes(), old_tdd)
        self.assertEqual(list((self.tasks.state_path() / 'approval-history').iterdir()), [])

    def test_repeated_replacement_and_other_task_evidence_are_isolated(self):
        first_tdd = self._write_tdd('task-a-first')
        self.tasks.create('task-b')
        self._write_drafts('task-b')
        self.approval.approve()
        task_b_tdd = self._write_tdd('task-b')
        task_b_path = self.tasks.state_path() / 'tdd.json'

        self.tasks.use('task-a')
        self._write_drafts('B')
        first = self.approval.approve(replace_existing=True)
        self._write_tdd('task-a-second')
        second_tdd = (self.tasks.state_path() / 'tdd.json').read_bytes()
        self._write_drafts('C')
        second = self.approval.approve(replace_existing=True)

        self.assertEqual((first.archive / 'tdd.json').read_bytes(), first_tdd)
        self.assertEqual((second.archive / 'tdd.json').read_bytes(), second_tdd)
        self.assertFalse((self.tasks.state_path() / 'tdd.json').exists())
        self.tasks.use('task-b')
        self.assertEqual(task_b_path.read_bytes(), task_b_tdd)
        self.assertEqual(
            json.loads(task_b_path.read_text())['baseline'],
            self.approval.validate_baseline(),
        )

    def test_identical_replace_is_noop_and_preserves_current_evidence(self):
        old_tdd = self._write_tdd('still-current')
        state = self.tasks.state_path()
        receipt = state / 'verify.json'
        review = state / 'review-current.json'
        receipt.write_bytes(b'{"current": true}\n')
        review.write_bytes(b'{"review_id": "current"}\n')
        spec, plan = self.tasks.draft_paths()
        approved_spec, approved_plan = self.tasks.working_paths()
        spec.write_bytes(approved_spec.read_bytes())
        plan.write_bytes(approved_plan.read_bytes())
        old_baseline = self.approval.validate_baseline()

        outcome = self.approval.approve(replace_existing=True)

        self.assertEqual(outcome.kind, 'existing')
        self.assertEqual(outcome.baseline, old_baseline)
        self.assertIsNone(outcome.archive)
        self.assertEqual((state / 'tdd.json').read_bytes(), old_tdd)
        self.assertEqual(receipt.read_bytes(), b'{"current": true}\n')
        self.assertEqual(review.read_bytes(), b'{"review_id": "current"}\n')
        self.assertFalse((state / 'approval-history').exists())


if __name__ == '__main__':
    unittest.main()
