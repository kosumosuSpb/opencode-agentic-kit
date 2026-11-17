from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

from runner import agentic_gate
from runner.agentic import cli
from runner.agentic.commandline import CommandDispatcher, build_parser

RUNNER = Path(__file__).resolve().parents[1] / 'runner/agentic_gate.py'


class RunnerCliContractTests(unittest.TestCase):
    def test_entrypoint_delegates_to_component_cli(self):
        self.assertIs(agentic_gate.main, cli.main)

    def run_runner(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(RUNNER), *args],
            cwd=cwd,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_help_contract_from_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_runner('--help', cwd=Path(tmp))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, '')
        for command in (
            'approve',
            'task-info',
            'gate',
            'ready',
            'tdd',
            'review-context',
            'accept',
        ):
            self.assertIn(command, result.stdout)

    def test_argparse_usage_error_is_exit_code_two(self):
        result = self.run_runner('gate', 'unsupported')

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertIn('invalid choice', result.stderr)

    def test_parser_preserves_every_public_command_and_significant_arguments(self):
        parser = build_parser()
        examples = {
            'approve': ['approve', '--human-confirm', '--replace-existing'],
            'check-approval': ['check-approval'],
            'task-ensure': ['task-ensure'],
            'task-info': ['task-info'],
            'implement-context': ['implement-context'],
            'task-new': ['task-new', 'new-task'],
            'task-use': ['task-use', 'old-task'],
            'task-reopen': ['task-reopen', 'done-task', '--human-confirm'],
            'task-status': ['task-status'],
            'complete': ['complete'],
            'fingerprint': ['fingerprint'],
            'baseline': ['baseline'],
            'gate': ['gate', 'full'],
            'ready': ['ready', 'verify'],
            'tdd': [
                'tdd',
                '--cycle',
                'cycle-1',
                '--stage',
                'red',
                '--scope',
                'regression',
                '--expect',
                'expected failure',
                'python',
                '-m',
                'pytest',
            ],
            'evidence': ['evidence'],
            'review-context': ['review-context', 'full'],
            'review-start': ['review-start', 'finalize'],
            'review-status': ['review-status'],
            'review-submit': ['review-submit', 'review-1', 'reject', 'P1:file.py:12:bug'],
            'accept': ['accept', '--human-confirm'],
        }

        parsed = {name: parser.parse_args(arguments) for name, arguments in examples.items()}

        self.assertEqual(set(parsed), {args.command for args in parsed.values()})
        self.assertEqual(parsed['gate'].level, 'full')
        self.assertEqual(parsed['tdd'].test_command, ['python', '-m', 'pytest'])
        self.assertEqual(parsed['review-submit'].findings, ['P1:file.py:12:bug'])
        self.assertIsNone(parsed['accept'].review_id)

    def test_dispatcher_routes_by_command_without_branching_into_other_handlers(self):
        calls: list[tuple[str, Path, str | None]] = []
        project = Path('/project')
        dispatcher = CommandDispatcher(
            {
                'gate': lambda root, args: calls.append(('gate', root, args.level)),
                'evidence': lambda root, _args: calls.append(('evidence', root, None)),
            }
        )

        dispatcher.dispatch(project, build_parser().parse_args(['gate', 'verify']))

        self.assertEqual(calls, [('gate', project, 'verify')])

    def test_gate_failure_is_exit_code_one_and_uses_fail_prefix(self):
        script = (
            'from pathlib import Path\n'
            'from unittest.mock import patch\n'
            'from runner.agentic import cli\n'
            "with patch.object(cli, 'validate_git_root'), "
            "patch.object(cli, 'gate', side_effect=cli.GateFailure('quality defect')):\n"
            "    raise SystemExit(cli.main(['--project', str(Path.cwd()), 'gate', 'fast']))\n"
        )
        result = subprocess.run(
            [sys.executable, '-c', script],
            cwd=RUNNER.parents[1],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr.strip(), 'FAIL: quality defect')

    def test_main_reports_gate_failure_in_process(self):
        stderr = StringIO()
        with (
            patch.object(cli, 'validate_git_root'),
            patch.object(cli, 'gate', side_effect=cli.GateFailure('quality defect')),
            redirect_stderr(stderr),
        ):
            result = cli.main(['--project', str(Path.cwd()), 'gate', 'fast'])

        self.assertEqual(result, 1)
        self.assertEqual(stderr.getvalue().strip(), 'FAIL: quality defect')

    def test_main_routes_tdd_arguments_to_the_existing_recorder_contract(self):
        recorder = Mock()
        with (
            patch.object(cli, 'validate_git_root'),
            patch.object(cli, 'record_tdd', recorder),
        ):
            result = cli.main(
                [
                    '--project',
                    str(Path.cwd()),
                    'tdd',
                    '--cycle',
                    'fix-1',
                    '--stage',
                    'red',
                    '--scope',
                    'regression',
                    '--expect',
                    'assertion',
                    'python',
                    '-m',
                    'pytest',
                ]
            )

        self.assertEqual(result, 0)
        recorder.assert_called_once_with(
            Path.cwd(),
            'fix-1',
            'red',
            ['python', '-m', 'pytest'],
            'assertion',
            scope='regression',
        )

    def test_main_preserves_review_submit_json_output(self):
        output = StringIO()
        submitted = {'review_id': 'review-1', 'verdict': 'reject'}
        with (
            patch.object(cli, 'validate_git_root'),
            patch.object(cli, 'submit_review', return_value=submitted) as submit,
            redirect_stdout(output),
        ):
            result = cli.main(
                [
                    '--project',
                    str(Path.cwd()),
                    'review-submit',
                    'review-1',
                    'reject',
                    'P1:file.py:12:bug',
                ]
            )

        self.assertEqual(result, 0)
        submit.assert_called_once_with(Path.cwd(), 'review-1', 'reject', ['P1:file.py:12:bug'])
        self.assertEqual(
            output.getvalue(), '{\n  "review_id": "review-1",\n  "verdict": "reject"\n}\n'
        )

    def test_human_confirmation_is_checked_before_mutating_commands(self):
        cases = (
            ('approve', ['approve']),
            ('reopen_task', ['task-reopen', 'task-1']),
            ('accept_review', ['accept', 'review-1']),
        )
        for operation, arguments in cases:
            with self.subTest(command=arguments[0]):
                action = Mock()
                stderr = StringIO()
                with (
                    patch.object(cli, 'validate_git_root'),
                    patch.object(cli, operation, action),
                    redirect_stderr(stderr),
                ):
                    result = cli.main(['--project', str(Path.cwd()), *arguments])

                self.assertEqual(result, 2)
                self.assertEqual(
                    stderr.getvalue().strip(),
                    'BLOCKED: HUMAN_REQUIRED: --human-confirm обязателен',
                )
                action.assert_not_called()

    def test_accept_routes_optional_review_id_after_human_confirmation(self):
        for arguments, expected in (
            (['accept', '--human-confirm'], None),
            (['accept', 'a' * 32, '--human-confirm'], 'a' * 32),
        ):
            with self.subTest(arguments=arguments):
                output = StringIO()
                with (
                    patch.object(cli, 'validate_git_root'),
                    patch.object(cli, 'accept_review', return_value={'accepted': True}) as accept,
                    redirect_stdout(output),
                ):
                    result = cli.main(['--project', str(Path.cwd()), *arguments])
                self.assertEqual(result, 0)
                accept.assert_called_once_with(Path.cwd(), expected)

    def test_blocked_runtime_error_is_exit_code_two_and_uses_blocked_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self.run_runner('--project', tmp, 'check-approval')

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, '')
        self.assertIn('BLOCKED: NOT_GIT_REPOSITORY', result.stderr)


if __name__ == '__main__':
    unittest.main()
