#!/usr/bin/env python3
"""Deterministic checks and workflow state for OpenCode/Codex. Python 3.11+."""

from __future__ import annotations

import json
import subprocess
from argparse import Namespace
from pathlib import Path

from .acceptance_checks import AcceptanceCheckRecorder
from .approval import APPROVED_STATUS as APPROVED_STATUS
from .approval import ApprovalService
from .commandline import CommandDispatcher, run_cli
from .coverage import generate_python_coverage
from .errors import GateError
from .errors import GateFailure as GateFailure
from .evidence import EvidenceService
from .project import file_digest as sha
from .project import (
    fingerprint,
    validate_git_root,
)
from .project import state_dir as state_dir
from .quality import (
    CommandJournal,
    QualityRunner,
    QualityRunResult,
    default_tool_path,
    execute_command,
)
from .quality import baseline_test as run_baseline_test
from .receipts import ReceiptService
from .review import ReviewService
from .sources import source_targets
from .storage import JsonStorage
from .tasks import TaskService
from .tdd import TDDRecorder
from .tdd import validate_tdd_command as validate_tdd_command
from .wheel import WheelVerifier, wheel_applicability


def _task_service(project: Path) -> TaskService:
    return TaskService(project, JsonStorage())


def _approval_service(project: Path) -> ApprovalService:
    storage = JsonStorage()
    return ApprovalService(project, TaskService(project, storage), storage)


def registry_path(project: Path) -> Path:
    return _task_service(project).registry_path


def _write_json_atomic(path: Path, data: object) -> None:
    JsonStorage().write(path, data)


def load_registry(project: Path, *, create: bool = False) -> dict[str, object]:
    return _task_service(project).load(create=create)


def active_task(project: Path, *, create: bool = False) -> tuple[str, dict[str, object]]:
    return _task_service(project).active(create=create)


def task_state_dir(project: Path) -> Path:
    return _task_service(project).state_path()


def task_new(project: Path, task_id: str) -> None:
    print(_task_service(project).create(task_id))


def task_use(project: Path, task_id: str) -> None:
    print(_task_service(project).use(task_id))


def task_status(project: Path) -> None:
    try:
        status = _task_service(project).load()
    except GateError as exc:
        if str(exc) != 'NO_ACTIVE_TASK: сначала /design или /task new <id>':
            raise
        status = {'version': 2, 'active_task': None, 'tasks': {}}

    print(json.dumps(status, indent=2))


def task_info(project: Path, *, create: bool = False) -> None:
    print(json.dumps(_task_service(project).info(create=create)))


def require_open_task(project: Path) -> str:
    return _task_service(project).require_open()


def reject_symlink_ancestors(path: Path, root: Path) -> None:
    JsonStorage().reject_symlink_ancestors(path, root)


def approved_paths(project: Path) -> tuple[Path, Path]:
    return _task_service(project).baseline_paths()


def working_paths(project: Path) -> tuple[Path, Path]:
    return _task_service(project).working_paths()


def draft_paths(project: Path) -> tuple[Path, Path]:
    return _task_service(project).draft_paths()


def validate_baseline(project: Path, *, require_approved_status: bool = True) -> dict[str, str]:
    return _approval_service(project).validate_baseline(
        require_approved_status=require_approved_status
    )


def approve(project: Path, *, replace_existing: bool = False) -> None:
    outcome = _approval_service(project).approve(replace_existing=replace_existing)
    if outcome.kind == 'existing':
        print('APPROVED: уже существует baseline, проверяем неизменность')
    elif outcome.kind == 'replaced':
        print(f'APPROVAL REPLACED BY HUMAN: previous approval archived at {outcome.archive}')
    else:
        print(
            'APPROVED (SOFT LOCK): создан snapshot; для жёсткой OS-изоляции нужен отдельный аккаунт'
        )
    print(json.dumps(outcome.baseline, indent=2))


def implement_context(project: Path) -> None:
    """Emit all deterministic context Developer needs before implementation."""
    task_id, task = active_task(project)
    if task.get('completed_at') is not None:
        raise GateError(f'TASK_COMPLETED: {task_id}; create or select an open task')
    baseline = validate_baseline(project)
    working = working_paths(project)
    backend, python = python_backend(project)

    def probe(command: list[str]) -> dict[str, object]:
        try:
            result = subprocess.run(
                command, cwd=project, capture_output=True, text=True, check=False, timeout=30
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {'command': command, 'available': False, 'output': str(exc)}
        return {
            'command': command,
            'available': result.returncode == 0,
            'output': (result.stdout or result.stderr).strip(),
        }

    if python[:3] == ['uv', 'run', '--locked']:
        # Context collection must not create/sync an environment merely to print versions.
        python_command = [*python, '--no-sync', 'python', '--version']
        pytest_command = [*backend, '--no-sync', 'pytest', '--version']
    else:
        python_command = [python[0], '--version']
        pytest_command = [*backend, 'pytest', '--version']
    test_command = [*backend, 'pytest', '-q']
    print(
        json.dumps(
            {
                'task_id': task_id,
                'completed_at': task.get('completed_at'),
                'approved_spec': str(working[0].relative_to(project)),
                'approved_plan': str(working[1].relative_to(project)),
                'approval': {'verified': True, **baseline},
                'python': {
                    'backend': backend,
                    'python_probe': probe(python_command),
                    'pytest_probe': probe(pytest_command),
                    'test_command': test_command,
                    'locked': backend == ['uv', 'run', '--locked'],
                },
            },
            indent=2,
        )
    )


def run(
    project: Path,
    command: list[str],
    *,
    capture: bool = False,
    env: dict[str, str] | None = None,
    echo_capture: bool = True,
) -> str:
    return execute_command(
        project,
        command,
        capture=capture,
        env=env,
        echo_capture=echo_capture,
    )


def _python_invocation(python: list[str], project: Path, *args: str) -> list[str]:
    from .quality import python_invocation

    return python_invocation(python, project, *args)


def wheel_check(
    project: Path,
    python: list[str],
    *,
    _journal: CommandJournal | None = None,
) -> dict[str, object]:
    journal = _journal or CommandJournal(run)
    verifier = WheelVerifier(journal, journal.entries, _python_invocation, sha)
    return verifier.verify(project, python)


def tool_path(name: str) -> Path:
    return default_tool_path(name)


def python_backend(project: Path) -> tuple[list[str], list[str]]:
    from .quality import python_backend as select_backend

    return select_backend(project)


def require_module(project: Path, runner: list[str], module: str) -> None:
    from .quality import require_module as check_module

    check_module(project, runner, module)


def pytest_command(backend: list[str], *args: str) -> list[str]:
    from .quality import pytest_command as build_pytest_command

    return build_pytest_command(backend, *args)


def python_coverage(
    project: Path,
    backend: list[str],
    python: list[str],
    sources: list[str],
    report: Path,
) -> None:
    generate_python_coverage(
        project,
        backend,
        python,
        sources,
        report,
        executor=CommandJournal(run),
        python_invocation=_python_invocation,
        pytest_builder=pytest_command,
    )


def mutator_command(project: Path, backend: list[str], sources: list[str]) -> list[str]:
    from .quality import mutator_command as build_mutator_command

    return build_mutator_command(project, backend, sources, tool_path)


def _quality_runner(project: Path) -> QualityRunner:
    storage = JsonStorage()
    tasks = TaskService(project, storage)
    approval = ApprovalService(project, tasks, storage)
    journal = CommandJournal(run)

    return QualityRunner(
        project,
        tasks,
        approval,
        journal,
        discover_sources=source_targets,
        select_backend=python_backend,
        check_module=require_module,
        resolve_tool=tool_path,
        wheel_applicability_check=wheel_applicability,
        wheel_check=lambda root, python: wheel_check(root, python, _journal=journal),
    )


def quality(project: Path, level: str, runner: QualityRunner | None = None) -> dict[str, object]:
    return (runner or _quality_runner(project)).run(level).wheel


def baseline_test(project: Path) -> None:
    storage = JsonStorage()
    tasks = TaskService(project, storage)
    approval = ApprovalService(project, tasks, storage)
    run_baseline_test(
        project,
        tasks,
        approval,
        CommandJournal(run),
        select_backend=python_backend,
        check_module=require_module,
    )


def _receipt_service(project: Path) -> ReceiptService:
    storage = JsonStorage()
    tasks = TaskService(project, storage)
    approval = ApprovalService(project, tasks, storage)
    return ReceiptService(project, tasks, approval, storage)


def receipt_path(project: Path, level: str) -> Path:
    return _receipt_service(project).path(level)


def gate(project: Path, level: str) -> None:
    runner = _quality_runner(project)

    def perform() -> QualityRunResult:
        wheel = quality(project, level, runner)
        return QualityRunResult(wheel=wheel, commands=runner.journal.snapshot())

    _receipt_service(project).run_gate(level, perform, runner.journal.snapshot)


def current_receipt(project: Path, level: str) -> dict[str, object]:
    return _receipt_service(project).current(level)


def check_receipt(project: Path, level: str) -> None:
    current_receipt(project, level)
    print(f'PASS: {level} проверен для текущего состояния Git/worktree')


def _evidence_service(project: Path) -> EvidenceService:
    storage = JsonStorage()
    tasks = TaskService(project, storage)
    approval = ApprovalService(project, tasks, storage)
    receipts = ReceiptService(project, tasks, approval, storage)
    return EvidenceService(project, tasks, approval, receipts, storage)


def collect_evidence(project: Path) -> dict[str, object]:
    return _evidence_service(project).collect()


def evidence(project: Path) -> None:
    print(json.dumps(collect_evidence(project), indent=2))


def review_context(project: Path, level: str) -> None:
    print(json.dumps(_evidence_service(project).review_context(level), indent=2))


def _tdd_recorder(project: Path) -> TDDRecorder:
    storage = JsonStorage()
    tasks = TaskService(project, storage)
    approval = ApprovalService(project, tasks, storage)
    return TDDRecorder(project, tasks, approval, storage, process_runner=subprocess.run)


def tdd_path(project: Path) -> Path:
    return _tdd_recorder(project).path


def record_tdd(
    project: Path,
    cycle: str,
    stage: str,
    command: list[str],
    expected_failure: str | None = None,
    *,
    scope: str = 'feature',
) -> None:
    _tdd_recorder(project).record(cycle, stage, command, expected_failure, scope=scope)


def _review_service(project: Path) -> ReviewService:
    storage = JsonStorage()
    tasks = TaskService(project, storage)
    approval = ApprovalService(project, tasks, storage)
    return ReviewService(
        project,
        tasks,
        approval,
        storage,
        receipt_reader=lambda level: current_receipt(project, level),
        receipt_path=lambda level: receipt_path(project, level),
    )


def review_path(project: Path, review_id: str) -> Path:
    return _review_service(project).review_path(review_id)


def begin_review(project: Path, phase: str = 'finalize') -> dict[str, object]:
    return _review_service(project).begin(phase)


def review_status(project: Path) -> dict[str, object]:
    return _review_service(project).status()


def submit_review(
    project: Path,
    review_id: str,
    verdict: str,
    finding_tokens: list[str] | None = None,
) -> dict[str, object]:
    return _review_service(project).submit(review_id, verdict, finding_tokens)


def accept_review(project: Path, review_id: str | None = None) -> dict[str, object]:
    return _review_service(project).accept(review_id)


def reopen_task(project: Path, task_id: str) -> dict[str, object]:
    return _review_service(project).reopen(task_id)


def complete_task(project: Path) -> None:
    raise GateError('DIRECT_COMPLETE_DISABLED: use /finalize, wait for Reviewer, then /accept')


def _require_human_confirmation(args: Namespace) -> None:
    if not args.human_confirm:
        raise GateError('HUMAN_REQUIRED: --human-confirm обязателен')


def _approve_command(project: Path, args: Namespace) -> None:
    _require_human_confirmation(args)
    approve(project, replace_existing=args.replace_existing)


def _check_approval_command(project: Path, _args: Namespace) -> None:
    require_open_task(project)
    print(json.dumps(validate_baseline(project), indent=2))


def _task_ensure_command(project: Path, _args: Namespace) -> None:
    task_info(project, create=True)


def _task_info_command(project: Path, _args: Namespace) -> None:
    task_info(project)


def _implement_context_command(project: Path, _args: Namespace) -> None:
    implement_context(project)


def _task_new_command(project: Path, args: Namespace) -> None:
    task_new(project, args.task_id)


def _task_use_command(project: Path, args: Namespace) -> None:
    task_use(project, args.task_id)


def _task_reopen_command(project: Path, args: Namespace) -> None:
    _require_human_confirmation(args)
    print(json.dumps(reopen_task(project, args.task_id), indent=2))


def _task_status_command(project: Path, _args: Namespace) -> None:
    task_status(project)


def _complete_command(project: Path, _args: Namespace) -> None:
    complete_task(project)


def _fingerprint_command(project: Path, _args: Namespace) -> None:
    print(fingerprint(project))


def _baseline_command(project: Path, _args: Namespace) -> None:
    baseline_test(project)


def _gate_command(project: Path, args: Namespace) -> None:
    gate(project, args.level)


def _ready_command(project: Path, args: Namespace) -> None:
    check_receipt(project, args.level)


def _tdd_command(project: Path, args: Namespace) -> None:
    record_tdd(project, args.cycle, args.stage, args.test_command, args.expect, scope=args.scope)


def _check_command(project: Path, args: Namespace) -> None:
    storage = JsonStorage()
    tasks = TaskService(project, storage)
    approval = ApprovalService(project, tasks, storage)
    AcceptanceCheckRecorder(project, tasks, approval, storage).record(
        args.criterion, args.test_command
    )


def _evidence_command(project: Path, _args: Namespace) -> None:
    evidence(project)


def _review_context_command(project: Path, args: Namespace) -> None:
    review_context(project, args.level)


def _review_start_command(project: Path, args: Namespace) -> None:
    print(json.dumps(begin_review(project, args.phase), indent=2))


def _review_status_command(project: Path, _args: Namespace) -> None:
    print(json.dumps(review_status(project), indent=2))


def _review_submit_command(project: Path, args: Namespace) -> None:
    result = submit_review(project, args.review_id, args.verdict, args.findings)
    print(json.dumps(result, indent=2))


def _accept_command(project: Path, args: Namespace) -> None:
    _require_human_confirmation(args)
    print(json.dumps(accept_review(project, args.review_id), indent=2))


def _command_dispatcher() -> CommandDispatcher:
    return CommandDispatcher(
        {
            'approve': _approve_command,
            'check-approval': _check_approval_command,
            'task-ensure': _task_ensure_command,
            'task-info': _task_info_command,
            'implement-context': _implement_context_command,
            'task-new': _task_new_command,
            'task-use': _task_use_command,
            'task-reopen': _task_reopen_command,
            'task-status': _task_status_command,
            'complete': _complete_command,
            'fingerprint': _fingerprint_command,
            'baseline': _baseline_command,
            'gate': _gate_command,
            'ready': _ready_command,
            'tdd': _tdd_command,
            'check': _check_command,
            'evidence': _evidence_command,
            'review-context': _review_context_command,
            'review-start': _review_start_command,
            'review-status': _review_status_command,
            'review-submit': _review_submit_command,
            'accept': _accept_command,
        }
    )


def main(argv: list[str] | None = None) -> int:
    return run_cli(argv, _command_dispatcher(), validate_project=validate_git_root)


if __name__ == '__main__':
    raise SystemExit(main())
