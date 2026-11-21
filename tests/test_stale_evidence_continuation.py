from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from runner.agentic import cli
from runner.agentic.acceptance_checks import AcceptanceCheckRecorder
from runner.agentic.errors import GateError
from runner.agentic.storage import JsonStorage


def passing_quality(_project: Path, level: str, _runner=None) -> dict[str, object]:
    if level == 'fast':
        return {'applicable': None, 'status': 'not_run'}
    return {'applicable': False, 'status': 'not_applicable'}


class StaleEvidenceContinuationTests(unittest.TestCase):
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
        cli.task_new(self.project, 'continuation')
        spec, plan = cli.draft_paths(self.project)
        spec.write_text('# Spec\n\nAC1: VALUE is one.\n')
        plan.write_text('# Plan\n\nRun the continuation unittest.\n')
        cli.approve(self.project)
        (self.project / 'app.py').write_text('VALUE = 0\n')
        (self.project / 'test_continuation.py').write_text(
            'import unittest\n'
            'import app\n\n'
            'class ContinuationTest(unittest.TestCase):\n'
            '    def test_value(self):\n'
            '        self.assertEqual(app.VALUE, 1)\n'
        )
        self.command = [
            sys.executable,
            '-m',
            'unittest',
            'discover',
            '-s',
            '.',
            '-p',
            'test_continuation.py',
        ]

    def _record_complete_tdd(self) -> None:
        cli.record_tdd(
            self.project, 'value', 'red', self.command, '0 != 1', scope='feature'
        )
        cli.record_tdd(
            self.project, 'value', 'red', self.command, '0 != 1', scope='regression'
        )
        (self.project / 'app.py').write_text('VALUE = 1  # implemented\n')
        for stage in ('green', 'refactor'):
            for scope in ('feature', 'regression'):
                cli.record_tdd(self.project, 'value', stage, self.command, scope=scope)

    def _record_ac(self) -> None:
        storage = JsonStorage()
        AcceptanceCheckRecorder(
            self.project,
            cli._task_service(self.project),
            cli._approval_service(self.project),
            storage,
        ).record('AC1', self.command)

    def test_completed_tdd_survives_stale_evidence_and_fresh_review(self) -> None:
        self._record_complete_tdd()
        tdd_before = cli.tdd_path(self.project).read_bytes()
        self._record_ac()
        with patch.object(cli, 'quality', side_effect=passing_quality):
            cli.gate(self.project, 'verify')
        old_review = cli.begin_review(self.project, 'verify')

        (self.project / 'app.py').write_text('VALUE = 1  # lint/review fix\n')

        self.assertEqual(cli.tdd_path(self.project).read_bytes(), tdd_before)
        with self.assertRaisesRegex(GateError, 'STALE_VERIFY_RECEIPT'):
            cli.current_receipt(self.project, 'verify')
        stale = cli.collect_evidence(self.project)
        self.assertFalse(stale['receipts']['verify']['current'])
        context = cli._evidence_service(self.project).review_context
        with self.assertRaisesRegex(GateError, 'STALE_VERIFY_RECEIPT'):
            context('verify')
        with self.assertRaisesRegex(GateError, 'STALE_REVIEW'):
            cli.submit_review(self.project, old_review['review_id'], 'pass', [])

        self._record_ac()
        with patch.object(cli, 'quality', side_effect=passing_quality):
            cli.gate(self.project, 'verify')
        review_context = cli._evidence_service(self.project).review_context('verify')
        ac_records = review_context['acceptance_checks']['records']
        self.assertFalse(ac_records[0]['current_pass'])
        self.assertTrue(ac_records[1]['current_pass'])
        self.assertEqual(cli.tdd_path(self.project).read_bytes(), tdd_before)

        new_review = cli.begin_review(self.project, 'verify')
        self.assertNotEqual(new_review['review_id'], old_review['review_id'])
        cli.submit_review(self.project, new_review['review_id'], 'pass', [])
        with patch.object(cli, 'quality', side_effect=passing_quality):
            cli.gate(self.project, 'full')
        final = cli.begin_review(self.project, 'finalize')
        self.assertEqual(final['source_review_id'], new_review['review_id'])
        cli.submit_review(self.project, final['review_id'], 'pass', [])

        accepted = cli.accept_review(self.project, final['review_id'])

        self.assertFalse(accepted['already_completed'])
        self.assertEqual(accepted['fingerprint'], cli.fingerprint(self.project))

    def test_implement_context_reports_completed_historical_tdd(self) -> None:
        self._record_complete_tdd()
        output = io.StringIO()
        original_run = subprocess.run

        def probe_or_run(command, *args, **kwargs):
            if command[-1:] == ['--version']:
                output = 'pytest\n' if 'pytest' in command else 'Python 3.13\n'
                return subprocess.CompletedProcess(command, 0, output, '')
            return original_run(command, *args, **kwargs)

        with (
            patch.object(
                cli,
                'python_backend',
                return_value=([sys.executable, '-m'], [sys.executable]),
            ),
            patch.object(cli.subprocess, 'run', side_effect=probe_or_run),
            redirect_stdout(output),
        ):
            cli.implement_context(self.project)

        context = json.loads(output.getvalue())
        self.assertTrue(context['workflow_state']['tdd']['has_completed_cycle'])
        self.assertTrue(context['workflow_state']['tdd']['all_recorded_cycles_complete'])
        self.assertTrue(context['workflow_state']['tdd']['baseline_current'])
        self.assertTrue(context['workflow_state']['tdd']['cycles']['value']['complete'])
        self.assertNotIn('records', context['workflow_state']['tdd']['cycles']['value'])
        self.assertEqual(context['workflow_state']['acceptance_checks']['historical_count'], 0)
        self.assertEqual(
            context['workflow_state']['tdd']['baseline'],
            cli.validate_baseline(self.project),
        )

    def test_mixed_cycles_are_not_reported_as_fully_complete(self) -> None:
        self._record_complete_tdd()
        (self.project / 'app.py').write_text('VALUE = 0  # next behavior absent\n')
        cli.record_tdd(
            self.project,
            'next-value',
            'red',
            self.command,
            '0 != 1',
            scope='feature',
        )

        state = cli._evidence_service(self.project).implementation_context()['tdd']

        self.assertTrue(state['baseline_current'])
        self.assertTrue(state['has_completed_cycle'])
        self.assertFalse(state['all_recorded_cycles_complete'])
        self.assertTrue(state['cycles']['value']['regression_complete'])
        self.assertFalse(state['cycles']['next-value']['complete'])

    def test_tdd_baseline_freshness_preserves_legacy_semantics(self) -> None:
        self._record_complete_tdd()
        path = cli.tdd_path(self.project)
        document = json.loads(path.read_text())
        document['baseline'] = {'task_id': 'different-approval'}
        path.write_text(json.dumps(document))

        service = cli._evidence_service(self.project)
        self.assertFalse(service.implementation_context()['tdd']['baseline_current'])

        document.pop('baseline')
        path.write_text(json.dumps(document))
        self.assertTrue(service.implementation_context()['tdd']['baseline_current'])

        (cli.task_state_dir(self.project) / 'approval-history').mkdir()
        self.assertFalse(service.implementation_context()['tdd']['baseline_current'])


if __name__ == '__main__':
    unittest.main()
