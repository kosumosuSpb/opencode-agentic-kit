"""Acceptance criterion identifiers are rejected before approval mutation."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runner.agentic.acceptance_criteria import validate_criterion_definitions
from runner.agentic.approval import APPROVED_STATUS, ApprovalService
from runner.agentic.errors import GateError
from runner.agentic.storage import JsonStorage
from runner.agentic.tasks import TaskService


class AcceptanceCriterionDefinitionTests(unittest.TestCase):
    def test_detects_invalid_ids_with_horizontal_space_before_separator(self):
        definitions = (
            '- AC-1: description',
            '- AC-1 — description',
            '### AC-1 — description',
            '| AC-1 | description |',
            '- AC-1 : description',
            '- AC-1\t— description',
            '| AC-1\t| description |',
        )

        for definition in definitions:
            with (
                self.subTest(definition=definition),
                self.assertRaisesRegex(GateError, r':1: AC-1;'),
            ):
                validate_criterion_definitions(definition, 'draft.md')

    def test_ignores_prose_and_does_not_cross_line_boundaries(self):
        prose = (
            'The historical AC-1 — notation must not be emitted.',
            'Compare AC-1 : with the canonical form.',
            'A table may mention | AC-1 | inside prose.',
            'AC-1\n— description on another line',
            '- AC-1\n: description on another line',
        )

        for text in prose:
            with self.subTest(text=text):
                validate_criterion_definitions(text, 'draft.md')


class AcceptanceCriteriaApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
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
        self.tasks.create('ac-contract')
        self.approval = ApprovalService(self.project, self.tasks, self.storage)

    def _write_drafts(self, spec: str, plan: str) -> None:
        for path, content in zip(self.tasks.draft_paths(), (spec, plan)):
            path.write_text(content)

    def test_accepts_canonical_ids_and_documents_without_definitions(self):
        self._write_drafts(
            '# Spec\n\n- AC1: first\n- **AC2:** second\n### AC10: tenth\n',
            '# Plan\n\n1. AC1: test first\n2. AC2: test second\n3. AC10: test tenth\n',
        )
        self.assertEqual(self.approval.approve().kind, 'created')

        # A separate task proves legacy/minimal documents remain accepted.
        self.tasks.create('minimal')
        self._write_drafts('# Minimal spec\n', '# Minimal plan\n')
        self.assertEqual(self.approval.approve().kind, 'created')

    def test_historical_approved_documents_are_not_revalidated_or_rewritten(self):
        historical = f'# Historical\n\n{APPROVED_STATUS}\n\n- AC-1: legacy\n'.encode()
        for path in (*self.tasks.working_paths(), *self.tasks.baseline_paths()):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(historical)

        self.approval.validate_baseline()

        for path in (*self.tasks.working_paths(), *self.tasks.baseline_paths()):
            self.assertEqual(path.read_bytes(), historical)

    def test_rejects_invalid_id_in_draft_spec_before_baseline_creation(self):
        spec = '# Spec\n\nIntro.\n- AC-1: invalid criterion\n'
        self._write_drafts(spec, '# Plan\n\n- AC1: valid criterion\n')

        with self.assertRaisesRegex(
            GateError,
            r'INVALID_ACCEPTANCE_CRITERION: .*drafts/spec\.md:4: AC-1; '
            r'expected AC\[1-9\]\[0-9\]\{0,3\}',
        ):
            self.approval.approve()

        self.assertFalse(any(path.exists() for path in self.tasks.baseline_paths()))
        self.assertFalse(any(path.exists() for path in self.tasks.working_paths()))

    def test_rejects_invalid_id_in_draft_plan(self):
        self._write_drafts(
            '# Spec\n\n- AC1: valid criterion\n',
            '# Plan\n\n- AC_1: invalid criterion\n',
        )

        with self.assertRaisesRegex(
            GateError, r'INVALID_ACCEPTANCE_CRITERION: .*drafts/plan\.md:3: AC_1'
        ):
            self.approval.approve()

    def test_rejects_zero_identifier(self):
        self._write_drafts('# Spec\n\nAC0: invalid criterion\n', '# Plan\n')

        with self.assertRaisesRegex(GateError, r':3: AC0;'):
            self.approval.approve()

    def test_ordinary_text_mentions_do_not_define_criteria(self):
        self._write_drafts(
            '# Spec\n\nThe historical AC-1 notation must not be emitted.\n',
            '# Plan\n\nCompare AC_1 in prose, but define no criterion here.\n',
        )

        self.assertEqual(self.approval.approve().kind, 'created')

    def test_invalid_replace_preserves_approval_and_all_evidence(self):
        self._write_drafts('# Spec A\n\n- AC1: valid\n', '# Plan A\n\n- AC1: valid\n')
        self.approval.approve()
        before_approval = {
            path: path.read_bytes()
            for path in (*self.tasks.working_paths(), *self.tasks.baseline_paths())
        }
        evidence = {}
        for name in (
            'tdd.json',
            'fast.json',
            'verify.json',
            'full.json',
            'acceptance-checks.json',
            'review-current.json',
        ):
            path = self.tasks.state_path() / name
            path.write_text(f'{{"marker": "{name}"}}\n')
            evidence[path] = path.read_bytes()
        self._write_drafts(
            '# Spec B\n\n- AC1: valid\n',
            '# Plan B\n\n- AC-1: invalid\n',
        )

        with self.assertRaisesRegex(GateError, 'INVALID_ACCEPTANCE_CRITERION'):
            self.approval.approve(replace_existing=True)

        self.assertEqual(
            {path: path.read_bytes() for path in before_approval}, before_approval
        )
        self.assertEqual({path: path.read_bytes() for path in evidence}, evidence)
        self.assertFalse((self.tasks.state_path() / 'approval-history').exists())


if __name__ == '__main__':
    unittest.main()
