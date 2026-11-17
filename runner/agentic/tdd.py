"""TDD evidence recording and RED/GREEN/REFACTOR transition rules."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .approval import ApprovalService
from .errors import GateError, GateFailure
from .project import fingerprint
from .storage import JsonStorage
from .tasks import TaskService

STAGES = ('red', 'green', 'refactor')


def _validate_executable_path(project: Path, invoked: str) -> None:
    if '/' not in invoked and '\\' not in invoked:
        return
    trusted = {
        str(Path(sys.executable).absolute()),
        str((project / '.venv/bin/python').absolute()),
        str((project / '.venv/bin/pytest').absolute()),
        str((project / '.venv/bin/py.test').absolute()),
    }
    if not Path(invoked).is_absolute() or str(Path(invoked).absolute()) not in trusted:
        raise GateError('TDD_COMMAND_DENIED: executable must be a selected Python/pytest backend')


def _is_test_command(executable: str, args: list[str]) -> bool:
    if executable in {'pytest', 'py.test'}:
        return True
    if executable.startswith('python'):
        return args[:2] in (['-m', 'pytest'], ['-m', 'unittest'])
    if executable != 'uv':
        return False
    return args[:3] == ['run', '--locked', 'pytest'] or args[:5] in (
        ['run', '--locked', 'python', '-m', 'pytest'],
        ['run', '--locked', 'python', '-m', 'unittest'],
    )


def validate_tdd_command(project: Path, command: list[str]) -> None:
    invoked = command[0]
    _validate_executable_path(project, invoked)
    executable = Path(invoked).name
    args = command[1:]
    if not _is_test_command(executable, args):
        raise GateError(
            'TDD_COMMAND_DENIED: use pytest/unittest directly, via Python -m, or uv run --locked'
        )


class TDDRecorder:
    def __init__(
        self,
        project: Path,
        tasks: TaskService,
        approval: ApprovalService,
        storage: JsonStorage,
        process_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.project = project
        self.tasks = tasks
        self.approval = approval
        self.storage = storage
        self._run = process_runner
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def path(self) -> Path:
        return self.tasks.state_path() / 'tdd.json'

    @contextmanager
    def _locked_path(self):
        state = self.tasks.state_path()
        state.mkdir(parents=True, mode=0o700, exist_ok=True)
        lock_path = state / 'tdd.lock'
        flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | getattr(os, 'O_NOFOLLOW', 0)
        try:
            descriptor = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise GateError(f'TDD_LOCK_FAILED: cannot open {lock_path}') from exc
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as exc:
                raise GateError(f'TDD_LOCK_FAILED: cannot lock {lock_path}') from exc
            yield state / 'tdd.json'
        finally:
            os.close(descriptor)

    def _load(
        self, baseline: dict[str, str], path: Path
    ) -> tuple[dict[str, object], dict[str, object]]:
        try:
            data = self._read_log(baseline, path)
            if not isinstance(data, dict):
                raise TypeError
            self._bind_baseline(data, baseline)
            if data.get('baseline') != baseline:
                raise GateError(
                    'TDD_BASELINE_CHANGED: prior TDD is retained for audit; '
                    'do not manufacture retrospective evidence'
                )
            cycles = data.get('cycles')
            if not isinstance(cycles, dict):
                raise TypeError
            return data, cycles
        except GateError:
            raise
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise GateError('INVALID_TDD_LOG') from exc

    def _read_log(self, baseline: dict[str, str], path: Path) -> object:
        if not path.exists():
            return {'baseline': baseline, 'cycles': {}}
        return json.loads(path.read_text())

    def _bind_baseline(self, data: dict[str, object], baseline: dict[str, str]) -> None:
        if 'baseline' in data:
            return
        if (self.tasks.state_path() / 'approval-history').exists():
            raise GateError(
                'TDD_BASELINE_CHANGED: legacy TDD predates the current approval; '
                'retained evidence must not be rebound'
            )
        data['baseline'] = baseline

    def _entries(self, cycles: dict[str, object], cycle: str) -> list[dict[str, object]]:
        entries = cycles.setdefault(cycle, [])
        if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
            raise GateError('INVALID_TDD_LOG')
        return entries

    def _validate_transition(
        self, entries: list[dict[str, object]], stage: str, scope: str
    ) -> None:
        target_index = STAGES.index(stage)
        if not entries:
            if stage != 'red':
                raise GateError(f'TDD_ORDER: {stage} requires a prior RED feature check')
            return
        try:
            current_stage = entries[-1]['stage']
            current_index = STAGES.index(str(current_stage))
            current_scopes = {
                entry.get('scope', 'feature')
                for entry in entries
                if entry.get('stage') == current_stage
            }
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise GateError('INVALID_TDD_LOG') from exc
        if target_index == current_index:
            if scope in current_scopes:
                raise GateError(f'TDD_SCOPE_DUPLICATE: {stage}/{scope} is already recorded')
            return
        if target_index == current_index + 1 and 'feature' in current_scopes:
            return
        raise GateError(f'TDD_ORDER: cannot record {stage}/{scope} after {current_stage}')

    def _execute(self, command: list[str], stage: str) -> subprocess.CompletedProcess[str]:
        print(f'$ (cd {shlex.quote(str(self.project))} && {shlex.join(command)})', flush=True)
        try:
            done = self._run(
                command,
                cwd=self.project,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GateError(f'MISSING_EXECUTABLE: {command[0]}') from exc
        print(done.stdout, end='', flush=True)
        expected_code = 1 if stage == 'red' else 0
        if done.returncode != expected_code:
            raise GateFailure(
                f'TDD_{stage.upper()}_UNEXPECTED_EXIT: '
                f'expected {expected_code}, got {done.returncode}'
            )
        return done

    def record(
        self,
        cycle: str,
        stage: str,
        command: list[str],
        expected_failure: str | None = None,
        *,
        scope: str = 'feature',
    ) -> None:
        command, expected_failure = self._validate_request(
            cycle, stage, scope, command, expected_failure
        )
        with self._locked_path() as path:
            task_id = self.tasks.require_open()
            baseline = self.approval.validate_baseline()
            data, cycles = self._load(baseline, path)
            entries = self._entries(cycles, cycle)
            self._validate_transition(entries, stage, scope)
            before = fingerprint(self.project)
            done = self._execute(command, stage)
            entry = self._evidence_entry(stage, scope, command, done, before, expected_failure)
            if self.tasks.require_open() != task_id or self.tasks.state_path() != path.parent:
                raise GateError('TDD_TASK_CHANGED: active task changed while recording evidence')
            entries.append(entry)
            self.storage.write(path, data)
        print(f'TDD EVIDENCE: {cycle} {stage.upper()} {scope.upper()} recorded')

    def _validate_request(
        self,
        cycle: str,
        stage: str,
        scope: str,
        command: list[str],
        expected_failure: str | None,
    ) -> tuple[list[str], str | None]:
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', cycle):
            raise GateError(
                'INVALID_CYCLE: use 1-64 ASCII letters, digits, dot, underscore or dash'
            )
        if scope not in {'feature', 'regression'}:
            raise GateError('INVALID_TDD_SCOPE: use feature or regression')
        if command[:1] == ['--']:
            command = command[1:]
        if not command:
            raise GateError('EMPTY_TDD_COMMAND')
        validate_tdd_command(self.project, command)
        expected_failure = expected_failure.strip() if expected_failure else None
        if stage == 'red' and (not expected_failure or len(expected_failure) > 500):
            raise GateError(
                'RED_EXPECTATION_REQUIRED: describe the expected failure in 1-500 characters'
            )
        return command, expected_failure

    def _evidence_entry(
        self,
        stage: str,
        scope: str,
        command: list[str],
        done: subprocess.CompletedProcess[str],
        before: str,
        expected_failure: str | None,
    ) -> dict[str, object]:
        entry: dict[str, object] = {
            'stage': stage,
            'scope': scope,
            'command': command,
            'exit_code': done.returncode,
            'output_sha256': hashlib.sha256(done.stdout.encode()).hexdigest(),
            'output_tail': done.stdout[-4000:],
            'fingerprint_before': before,
            'fingerprint_after': fingerprint(self.project),
            'recorded_at': self._clock().isoformat(timespec='seconds'),
        }
        if expected_failure:
            entry['expected_failure'] = expected_failure
        return entry
