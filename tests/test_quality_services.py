from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner.agentic.approval import ApprovalService
from runner.agentic.errors import GateError
from runner.agentic.project import fingerprint
from runner.agentic.quality import CommandJournal, QualityRunResult
from runner.agentic.receipts import ReceiptService
from runner.agentic.storage import JsonStorage
from runner.agentic.tasks import TaskService


class QualityServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Path(self.temp.name) / 'project'
        self.project.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.project)], check=True)
        (self.project / 'example.py').write_text('VALUE = 1\n')
        state = Path(self.temp.name) / 'state'
        environment = patch.dict(os.environ, {'AGENTIC_STATE_DIR': str(state)})
        environment.start()
        self.addCleanup(environment.stop)
        self.storage = JsonStorage()
        self.tasks = TaskService(self.project, self.storage)
        self.tasks.create('quality')
        for path, content in zip(self.tasks.draft_paths(), ('Spec', 'Plan')):
            path.write_text(content)
        self.approval = ApprovalService(self.project, self.tasks, self.storage)
        self.approval.approve()

    def test_command_journals_are_isolated_between_runs(self):
        first = CommandJournal(lambda project, command, **kwargs: 'first')
        second = CommandJournal(lambda project, command, **kwargs: 'second')

        first.run(self.project, ['tool', 'one'], capture=True)
        second.run(self.project, ['tool', 'two'], capture=True)

        self.assertEqual(first.entries[0]['command'], ['tool', 'one'])
        self.assertEqual(second.entries[0]['command'], ['tool', 'two'])
        self.assertEqual(len(first.entries), 1)
        self.assertEqual(len(second.entries), 1)

    def test_successful_receipt_uses_only_the_supplied_run_journal(self):
        receipts = ReceiptService(self.project, self.tasks, self.approval, self.storage)
        result = QualityRunResult(
            wheel={'applicable': False, 'status': 'not_applicable'},
            commands=[{'command': ['pytest'], 'exit_code': 0}],
        )

        receipt = receipts.run_gate('verify', lambda: result)

        self.assertTrue(receipt['passed'])
        self.assertEqual(receipt['commands'], result.commands)
        self.assertFalse(receipt['wheel_verified'])

    def test_atomic_write_failure_never_leaves_successful_receipt(self):
        receipts = ReceiptService(self.project, self.tasks, self.approval, self.storage)
        result = QualityRunResult(wheel={'applicable': None, 'status': 'not_run'}, commands=[])

        with (
            patch('runner.agentic.storage.os.replace', side_effect=OSError('interrupted')),
            self.assertRaisesRegex(OSError, 'interrupted'),
        ):
            receipts.run_gate('fast', lambda: result)

        self.assertFalse(receipts.path('fast').exists())

    def test_verify_and_full_reject_failed_not_run_and_contradictory_wheel_results(self):
        receipts = ReceiptService(self.project, self.tasks, self.approval, self.storage)
        invalid = (
            ('verify', {'applicable': True, 'status': 'failed'}),
            ('verify', {'applicable': None, 'status': 'not_run'}),
            ('verify', {'applicable': False, 'status': 'verified'}),
            ('full', {'applicable': True, 'status': 'not_applicable'}),
            ('full', {'applicable': True, 'status': 'failed'}),
        )

        for level, wheel in invalid:
            receipts.path(level).unlink(missing_ok=True)
            with (
                self.subTest(level=level, wheel=wheel),
                self.assertRaisesRegex(GateError, 'INVALID_WHEEL_RESULT'),
            ):
                receipts.run_gate(
                    level, lambda wheel=wheel: QualityRunResult(wheel=wheel, commands=[])
                )
            self.assertFalse(receipts.path(level).exists())

    def test_fast_accepts_not_run_wheel_result(self):
        receipts = ReceiptService(self.project, self.tasks, self.approval, self.storage)

        receipt = receipts.run_gate(
            'fast',
            lambda: QualityRunResult(wheel={'applicable': None, 'status': 'not_run'}, commands=[]),
        )

        self.assertTrue(receipt['passed'])
        self.assertFalse(receipt['wheel_verified'])

    def test_current_rejects_forged_passed_receipt_with_failed_wheel(self):
        receipts = ReceiptService(self.project, self.tasks, self.approval, self.storage)
        receipt = {
            'task_id': 'quality',
            'level': 'verify',
            'passed': True,
            'fingerprint': fingerprint(self.project),
            'baseline': self.approval.validate_baseline(),
            'wheel': {'applicable': True, 'status': 'failed'},
        }
        receipts.path('verify').parent.mkdir(parents=True, exist_ok=True)
        receipts.path('verify').write_text(json.dumps(receipt))

        with self.assertRaisesRegex(GateError, 'INVALID_WHEEL_RESULT'):
            receipts.current('verify')


if __name__ == '__main__':
    unittest.main()
