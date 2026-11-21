"""Behavioral regressions for non-TDD acceptance checks."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'runner'))
from agentic.acceptance_checks import AcceptanceCheckRecorder, resolve_check_command
from agentic.commandline import build_parser
from agentic.errors import GateError, GateFailure
from agentic.evidence import EvidenceService
from agentic.storage import JsonStorage


class FakeTasks:
    def __init__(self, root: Path) -> None:
        self.root = root

    def require_open(self) -> str:
        return 'acceptance-unittest'

    def state_path(self) -> Path:
        return self.root / 'state'


class FakeApproval:
    def __init__(self) -> None:
        self.baseline = {'task_id': 'acceptance-unittest', 'plan_sha256': 'p1'}

    def validate_baseline(self) -> dict[str, str]:
        return self.baseline


class AcceptanceCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = Path(self.tmp.name)
        (self.project / 'state').mkdir()
        (self.project / '.venv/bin').mkdir(parents=True)
        (self.project / '.venv/bin/python').touch()
        self.approval = FakeApproval()
        self.calls = []
        self.exit_code = 0

        def run(command, **kwargs):
            self.calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, self.exit_code, '3 tests OK\n')

        self.recorder = AcceptanceCheckRecorder(
            self.project, FakeTasks(self.project), self.approval, JsonStorage(),
            process_runner=run,
        )

    def test_portable_python_uses_selected_venv(self):
        command = resolve_check_command(
            self.project, ['python', '-m', 'unittest', 'discover', '-s', 'tests'],
        )
        self.assertEqual(command[0], str(self.project / '.venv/bin/python'))
        self.assertEqual(command[1:3], ['-m', 'unittest'])

    def test_disallows_relative_path_and_arbitrary_code(self):
        for command in (
            ['./.venv/bin/python', '-m', 'unittest'],
            ['python', '-c', 'print(1)'],
        ):
            with self.subTest(command=command), self.assertRaises(GateError):
                resolve_check_command(self.project, command)

    def test_records_pass_with_approval_and_fingerprint(self):
        with patch('agentic.acceptance_checks.fingerprint', return_value='worktree-a'):
            self.recorder.record('AC7', ['--', 'python', '-m', 'unittest', 'discover'])
        document = JsonStorage().read_object(self.recorder.path, 'BAD_LOG')
        entry = document['entries'][0]
        self.assertEqual(entry['criterion'], 'AC7')
        self.assertEqual(entry['exit_code'], 0)
        self.assertEqual(entry['fingerprint'], 'worktree-a')
        self.assertEqual(entry['baseline'], self.approval.baseline)
        self.assertEqual(self.calls[0][0][0], str(self.project / '.venv/bin/python'))
        self.assertNotIn('shell', self.calls[0][1])
        self.assertEqual(entry['baseline']['task_id'], self.recorder.tasks.require_open())

    def test_criterion_identifier_contract(self):
        with patch('agentic.acceptance_checks.fingerprint', return_value='worktree-a'):
            for criterion in ('AC1', 'AC2', 'AC10'):
                with self.subTest(criterion=criterion):
                    self.recorder.record(
                        criterion, ['python', '-m', 'unittest', 'discover']
                    )

        for criterion in ('AC-1', 'AC_1', 'AC0'):
            with (
                self.subTest(criterion=criterion),
                self.assertRaisesRegex(
                    GateError, r'INVALID_CRITERION: expected AC\[1-9\]\[0-9\]\{0,3\}'
                ),
            ):
                self.recorder.record(
                    criterion, ['python', '-m', 'unittest', 'discover']
                )

    def test_failed_check_is_recorded_and_returns_failure(self):
        self.exit_code = 1
        with (
            patch('agentic.acceptance_checks.fingerprint', return_value='worktree-a'),
            self.assertRaises(GateFailure),
        ):
            self.recorder.record('AC7', ['python', '-m', 'unittest', 'discover'])
        document = JsonStorage().read_object(self.recorder.path, 'BAD_LOG')
        self.assertEqual(document['entries'][0]['exit_code'], 1)

    def test_fingerprint_changes_fail_closed(self):
        with (
            patch('agentic.acceptance_checks.fingerprint', side_effect=['old', 'new']),
            self.assertRaisesRegex(GateError, 'CHECK_PROJECT_CHANGED'),
        ):
            self.recorder.record('AC7', ['python', '-m', 'unittest'])
        self.assertFalse(self.recorder.path.exists())

    def test_cli_parses_acceptance_check(self):
        args = build_parser().parse_args(
            ['check', '--criterion', 'AC7', '--', 'python', '-m', 'unittest', 'discover']
        )
        self.assertEqual(args.command, 'check')
        self.assertEqual(args.criterion, 'AC7')
        self.assertEqual(args.test_command[0], '--')

    def test_reviewer_freshness_and_latest_failure(self):
        service = EvidenceService(
            self.project, FakeTasks(self.project), self.approval, None, JsonStorage()
        )
        pass_entry = {
            'criterion': 'AC7', 'baseline': self.approval.baseline,
            'fingerprint': 'current', 'exit_code': 0,
            'command': ['/venv/bin/python', '-m', 'unittest'],
        }
        def evidence(entries, fp='current', baseline=None):
            return service._acceptance_check_evidence(
                {
                    'acceptance_checks': entries,
                    'current_fingerprint': fp,
                    'current_baseline': self.approval.baseline if baseline is None else baseline,
                }
            )['records']
        self.assertTrue(evidence([pass_entry])[0]['current_pass'])
        self.assertFalse(evidence([pass_entry], fp='changed')[0]['current_pass'])
        self.assertFalse(evidence([pass_entry], baseline={'plan_sha256': 'p2'})[0]['current_pass'])
        rows = evidence([pass_entry, {**pass_entry, 'exit_code': 1}])
        self.assertFalse(rows[0]['current_pass'])
        self.assertFalse(rows[1]['current_pass'])
        rows = evidence([pass_entry, {**pass_entry, 'exit_code': 0}])
        self.assertFalse(rows[0]['current_pass'])
        self.assertTrue(rows[1]['current_pass'])


if __name__ == '__main__':
    unittest.main()
