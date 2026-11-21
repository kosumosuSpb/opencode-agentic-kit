"""Runner-recorded, non-TDD acceptance checks for the current project state."""

from __future__ import annotations

import hashlib
import shlex
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .acceptance_criteria import CRITERION_FORMAT, is_canonical_criterion
from .approval import ApprovalService
from .errors import GateError, GateFailure
from .project import fingerprint
from .quality import python_backend
from .storage import JsonStorage
from .tasks import TaskService
from .tdd import validate_tdd_command


def resolve_check_command(project: Path, command: list[str]) -> list[str]:
    """Choose the selected project backend for portable *bare* tool names."""
    if command[:1] == ['--']:
        command = command[1:]
    if not command:
        raise GateError('EMPTY_CHECK_COMMAND')

    name = command[0]
    if name in {'python', 'python3', 'pytest', 'py.test'}:
        backend, python = python_backend(project)
        if name in {'python', 'python3'}:
            if python[:3] == ['uv', 'run', '--locked']:
                command = [*python, 'python', *command[1:]]
            else:
                command = [python[0], *command[1:]]
        elif backend[:3] == ['uv', 'run', '--locked']:
            command = [*backend, 'pytest', *command[1:]]
        else:
            command = [python[0], '-m', 'pytest', *command[1:]]

    # Same command whitelist and absolute-path trust boundary as Runner TDD.
    validate_tdd_command(project, command)
    return command


class AcceptanceCheckRecorder:
    def __init__(
        self,
        project: Path,
        tasks: TaskService,
        approval: ApprovalService,
        storage: JsonStorage,
        *,
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
        return self.tasks.state_path() / 'acceptance-checks.json'

    def record(self, criterion: str, raw_command: list[str]) -> None:
        if not is_canonical_criterion(criterion):
            raise GateError(f'INVALID_CRITERION: expected {CRITERION_FORMAT}')
        self.tasks.require_open()
        approved = self.approval.validate_baseline()
        command = resolve_check_command(self.project, raw_command)
        before = fingerprint(self.project)
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
        after = fingerprint(self.project)
        if before != after:
            raise GateError('CHECK_PROJECT_CHANGED: test changed tracked/project source files')

        entry = {
            'criterion': criterion,
            'command': command,
            'exit_code': done.returncode,
            'output_sha256': hashlib.sha256(done.stdout.encode()).hexdigest(),
            'output_tail': done.stdout[-4000:],
            'fingerprint': after,
            'baseline': approved,
            'recorded_at': self._clock().isoformat(timespec='seconds'),
        }
        if self.path.is_file():
            document = self.storage.read_object(self.path, 'INVALID_ACCEPTANCE_CHECKS')
            entries = document.get('entries')
            if document.get('version') != 1 or not isinstance(entries, list) or not all(
                isinstance(item, dict) for item in entries
            ):
                raise GateError('INVALID_ACCEPTANCE_CHECKS')
        else:
            document = {'version': 1, 'entries': []}
            entries = document['entries']
        if not isinstance(entries, list):
            raise GateError('INVALID_ACCEPTANCE_CHECKS')
        entries.append(entry)
        self.storage.write(self.path, document)
        if done.returncode != 0:
            raise GateFailure(f'ACCEPTANCE_CHECK_FAILED: {criterion}, exit {done.returncode}')
        print(f'ACCEPTANCE CHECK EVIDENCE: {criterion} PASS (current fingerprint)')
