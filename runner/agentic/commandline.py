"""Argument parsing, command dispatch, and process-level CLI semantics."""

from __future__ import annotations

import argparse
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from .errors import GateError, GateFailure

CommandHandler = Callable[[Path, argparse.Namespace], None]
ProjectValidator = Callable[[Path], None]


def build_parser() -> argparse.ArgumentParser:
    """Build the stable public command-line grammar."""
    parser = argparse.ArgumentParser(description='OpenCode/Codex deterministic gates')
    parser.add_argument('--project', type=Path, default=Path.cwd())
    command = parser.add_subparsers(dest='command', required=True)

    approve_parser = command.add_parser('approve')
    approve_parser.add_argument('--human-confirm', action='store_true')
    approve_parser.add_argument('--replace-existing', action='store_true')
    command.add_parser('check-approval')
    command.add_parser('task-ensure')
    command.add_parser('task-info')
    command.add_parser('implement-context')

    new_task = command.add_parser('task-new')
    new_task.add_argument('task_id')
    use_task = command.add_parser('task-use')
    use_task.add_argument('task_id')
    reopen = command.add_parser('task-reopen')
    reopen.add_argument('task_id')
    reopen.add_argument('--human-confirm', action='store_true')
    command.add_parser('task-status')
    command.add_parser('complete')
    command.add_parser('fingerprint')
    command.add_parser('baseline')

    gate = command.add_parser('gate')
    gate.add_argument('level', choices=['fast', 'verify', 'full'])
    ready = command.add_parser('ready')
    ready.add_argument('level', choices=['verify', 'full'])
    tdd = command.add_parser('tdd')
    tdd.add_argument('--cycle', required=True)
    tdd.add_argument('--stage', required=True, choices=['red', 'green', 'refactor'])
    tdd.add_argument('--scope', choices=['feature', 'regression'], default='feature')
    tdd.add_argument('--expect')
    tdd.add_argument('test_command', nargs=argparse.REMAINDER)

    check = command.add_parser('check')
    check.add_argument('--criterion', required=True)
    check.add_argument('test_command', nargs=argparse.REMAINDER)
    command.add_parser('evidence')
    review = command.add_parser('review-context')
    review.add_argument('level', choices=['verify', 'full'])
    start_review = command.add_parser('review-start')
    start_review.add_argument('phase', choices=['verify', 'finalize'])
    command.add_parser('review-status')
    submit = command.add_parser('review-submit')
    submit.add_argument('review_id')
    submit.add_argument('verdict', choices=['pass', 'reject', 'exception'])
    submit.add_argument('findings', nargs='*')
    accept = command.add_parser('accept')
    accept.add_argument('review_id', nargs='?')
    accept.add_argument('--human-confirm', action='store_true')
    return parser


class CommandDispatcher:
    """Route a parsed command to one explicitly registered handler."""

    def __init__(self, handlers: Mapping[str, CommandHandler]) -> None:
        self._handlers = dict(handlers)

    def dispatch(self, project: Path, args: argparse.Namespace) -> None:
        handler = self._handlers.get(args.command)
        if handler is None:
            raise GateError(f'UNKNOWN_COMMAND: {args.command}')
        handler(project, args)


def run_cli(
    argv: Sequence[str] | None,
    dispatcher: CommandDispatcher,
    *,
    validate_project: ProjectValidator,
) -> int:
    """Parse, validate the project boundary, dispatch, and map failures to exit codes."""
    args = build_parser().parse_args(argv)
    try:
        project = args.project.resolve(strict=True)
        validate_project(project)
        dispatcher.dispatch(project, args)
    except GateFailure as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        return 1
    except (GateError, OSError, subprocess.CalledProcessError) as exc:
        print('BLOCKED: ' + str(exc), file=sys.stderr)
        return 2
    return 0
