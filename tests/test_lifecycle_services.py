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
from runner.agentic.review import ReviewService
from runner.agentic.storage import JsonStorage
from runner.agentic.tasks import TaskService


class LifecycleServiceTests(unittest.TestCase):
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

    def test_task_registry_rejects_corruption_without_replacing_it(self):
        path = self.tasks.registry_path
        path.parent.mkdir(parents=True)
        path.write_text('{broken')

        with self.assertRaisesRegex(GateError, 'INVALID_TASK_REGISTRY'):
            self.tasks.load()

        self.assertEqual(path.read_text(), '{broken')

    def test_task_creation_is_atomic_and_duplicate_is_rejected(self):
        self.tasks.create('feature-a')
        snapshot = json.loads(self.tasks.registry_path.read_text())

        with self.assertRaisesRegex(GateError, 'TASK_EXISTS'):
            self.tasks.create('feature-a')

        self.assertEqual(json.loads(self.tasks.registry_path.read_text()), snapshot)

    def test_approval_service_rejects_changed_approved_copy(self):
        self.tasks.create('feature-a')
        drafts = self.tasks.draft_paths()
        drafts[0].write_text('Spec')
        drafts[1].write_text('Plan')
        approvals = ApprovalService(self.project, self.tasks, self.storage)

        approvals.approve()
        approvals.working_paths()[0].write_text('Changed')

        with self.assertRaisesRegex(GateError, 'SPEC_CHANGED'):
            approvals.validate_baseline()

    def test_reopen_rejects_corrupt_history_before_creating_archive(self):
        self.tasks.create('feature-a')
        registry = self.tasks.load()
        registry['tasks']['feature-a']['completed_at'] = '2026-11-15T00:00:00+00:00'
        registry['tasks']['feature-a']['status_history'] = {}
        self.storage.write(self.tasks.registry_path, registry)
        before = self.tasks.registry_path.read_bytes()
        approvals = ApprovalService(self.project, self.tasks, self.storage)
        reviews = ReviewService(
            self.project,
            self.tasks,
            approvals,
            self.storage,
            receipt_reader=lambda level: {},
            receipt_path=lambda level: self.state / f'{level}.json',
        )

        with self.assertRaisesRegex(GateError, 'status_history must be a list'):
            reviews.reopen('feature-a')

        self.assertEqual(self.tasks.registry_path.read_bytes(), before)
        self.assertFalse((self.tasks.state_path() / 'status-history').exists())

    def test_reopen_pointer_failure_does_not_reopen_registry(self):
        self.tasks.create('feature-a')
        registry = self.tasks.load()
        completed_at = '2026-11-15T00:00:00+00:00'
        registry['tasks']['feature-a']['completed_at'] = completed_at
        self.storage.write(self.tasks.registry_path, registry)
        approvals = ApprovalService(self.project, self.tasks, self.storage)
        reviews = ReviewService(
            self.project,
            self.tasks,
            approvals,
            self.storage,
            receipt_reader=lambda level: {},
            receipt_path=lambda level: self.state / f'{level}.json',
        )
        original_write = self.storage.write

        def interrupted_write(path, data):
            if path == reviews.current_path:
                raise OSError('interrupted pointer write')
            original_write(path, data)

        with (
            patch.object(self.storage, 'write', side_effect=interrupted_write),
            self.assertRaisesRegex(OSError, 'interrupted pointer write'),
        ):
            reviews.reopen('feature-a')

        task = self.tasks.load()['tasks']['feature-a']
        self.assertEqual(task['completed_at'], completed_at)

    def test_begin_write_failure_does_not_supersede_current_review(self):
        self.tasks.create('feature-a')
        for path, content in zip(self.tasks.draft_paths(), ('Spec', 'Plan')):
            path.write_text(content)
        approvals = ApprovalService(self.project, self.tasks, self.storage)
        approvals.approve()
        receipt = self.tasks.state_path() / 'full.json'
        receipt.write_text('{}')
        reviews = ReviewService(
            self.project,
            self.tasks,
            approvals,
            self.storage,
            receipt_reader=lambda level: {'passed_at': 'first'},
            receipt_path=lambda level: receipt,
            id_factory=lambda: 'a' * 32,
        )
        first = reviews.begin()
        receipt.write_text('{"changed": true}')
        interrupted = ReviewService(
            self.project,
            self.tasks,
            approvals,
            self.storage,
            receipt_reader=lambda level: {'passed_at': 'second'},
            receipt_path=lambda level: receipt,
            id_factory=lambda: 'b' * 32,
        )
        original_write = self.storage.write

        def interrupted_write(path, data):
            if path == interrupted.review_path('b' * 32):
                raise OSError('interrupted review write')
            original_write(path, data)

        with (
            patch.object(self.storage, 'write', side_effect=interrupted_write),
            self.assertRaisesRegex(OSError, 'interrupted review write'),
        ):
            interrupted.begin()

        record = json.loads(reviews.review_path(first['review_id']).read_text())
        self.assertEqual(record['status'], 'pending')


if __name__ == '__main__':
    unittest.main()
