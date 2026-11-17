"""Quality receipt persistence, invalidation, and freshness validation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .approval import ApprovalService
from .errors import GateError
from .project import fingerprint
from .quality import QualityRunResult
from .storage import JsonStorage
from .tasks import TaskService

Clock = Callable[[], datetime]
QualityCall = Callable[[], QualityRunResult]
CommandSnapshot = Callable[[], list[dict[str, object]]]


def valid_wheel_result(level: str, wheel: dict[str, object]) -> bool:
    """Return whether a successful gate may publish this wheel outcome."""
    status = wheel.get('status')
    applicable = wheel.get('applicable')
    if level == 'fast':
        return status == 'not_run' and applicable is None
    if level in {'verify', 'full'}:
        return (status == 'verified' and applicable is True) or (
            status == 'not_applicable' and applicable is False
        )
    return False


def validate_wheel_result(level: str, wheel: object) -> None:
    if not isinstance(wheel, dict) or not valid_wheel_result(level, wheel):
        raise GateError(f'INVALID_WHEEL_RESULT: {level} gate returned {wheel!r}')


def normalized_receipt_binding(
    data: dict[str, object], task_id: str, legacy: bool
) -> tuple[object, object]:
    receipt_task = data.get('task_id')
    if receipt_task is None and legacy:
        receipt_task = 'legacy'
    receipt_baseline = data.get('baseline')
    if legacy and isinstance(receipt_baseline, dict):
        receipt_baseline = {'task_id': 'legacy', **receipt_baseline}
    return receipt_task, receipt_baseline


class ReceiptService:
    def __init__(
        self,
        project: Path,
        tasks: TaskService,
        approval: ApprovalService,
        storage: JsonStorage,
        clock: Clock | None = None,
    ) -> None:
        self.project = project
        self.tasks = tasks
        self.approval = approval
        self.storage = storage
        self._clock = clock or (lambda: datetime.now(UTC))

    def path(self, level: str) -> Path:
        return self.tasks.state_path() / f'{level}.json'

    def run_gate(
        self,
        level: str,
        quality: QualityCall,
        command_snapshot: CommandSnapshot | None = None,
    ) -> dict[str, object]:
        task_id = self.tasks.require_open()
        self.approval.validate_baseline()
        receipt = self.path(level)
        receipt.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        receipt.unlink(missing_ok=True)
        before = fingerprint(self.project)
        try:
            result = quality()
        except GateError as exc:
            self._write_failure(level, task_id, exc, command_snapshot)
            raise
        after = fingerprint(self.project)
        if before != after:
            raise GateError(
                'WORKTREE_CHANGED_DURING_GATE: повтори проверку после стабилизации кода'
            )
        data = self._success(level, task_id, after, result)
        self.storage.write(receipt, data)
        return data

    def _write_failure(
        self,
        level: str,
        task_id: str,
        error: GateError,
        command_snapshot: CommandSnapshot | None,
    ) -> None:
        if not isinstance(error.wheel, dict):
            return
        failed = {
            'task_id': task_id,
            'level': level,
            'passed': False,
            'fingerprint': fingerprint(self.project),
            'baseline': self.approval.validate_baseline(),
            'commands': command_snapshot() if command_snapshot else [],
            'wheel': error.wheel,
            'wheel_verified': False,
            'error': str(error),
            'failed_at': self._clock().isoformat(timespec='seconds'),
        }
        self.storage.write(self.path(level), failed)

    def _success(
        self,
        level: str,
        task_id: str,
        current_fingerprint: str,
        result: QualityRunResult,
    ) -> dict[str, object]:
        wheel = result.wheel
        validate_wheel_result(level, wheel)
        return {
            'task_id': task_id,
            'level': level,
            'passed': True,
            'fingerprint': current_fingerprint,
            'baseline': self.approval.validate_baseline(),
            'commands': result.commands,
            'wheel': wheel,
            'wheel_verified': wheel.get('status') == 'verified',
            'passed_at': self._clock().isoformat(timespec='seconds'),
        }

    def read(
        self, level: str, error: str = 'INVALID_RECEIPT: damaged or incompatible receipt'
    ) -> dict[str, object]:
        return self.storage.read_object(self.path(level), error)

    def current(self, level: str) -> dict[str, object]:
        task_id, task = self.tasks.active()
        path = self.path(level)
        if not path.is_file():
            raise GateError(f'NO_{level.upper()}_RECEIPT: сначала пройди соответствующий gate')
        data = self.read(level)
        receipt_task, receipt_baseline = normalized_receipt_binding(
            data, task_id, task.get('layout') == 'legacy'
        )
        self._validate_current(level, data, task_id, receipt_task, receipt_baseline)
        return data

    def _validate_current(
        self,
        level: str,
        data: dict[str, object],
        task_id: str,
        receipt_task: object,
        receipt_baseline: object,
    ) -> None:
        if receipt_task != task_id:
            raise GateError(f'WRONG_TASK_RECEIPT: expected {task_id}, got {receipt_task}')
        if data.get('passed', True) is not True:
            raise GateError(f'FAILED_{level.upper()}_RECEIPT: gate did not pass')
        if 'wheel' in data:
            validate_wheel_result(level, data['wheel'])
        if 'fingerprint' not in data or 'baseline' not in data:
            raise GateError('INVALID_RECEIPT: damaged or incompatible receipt')
        if data['fingerprint'] != fingerprint(self.project):
            raise GateError(f'STALE_{level.upper()}_RECEIPT: код изменился после проверки')
        if receipt_baseline != self.approval.validate_baseline():
            raise GateError(f'STALE_{level.upper()}_RECEIPT: код изменился после проверки')

    def annotated(self, level: str) -> dict[str, object] | None:
        path = self.path(level)
        if not path.is_file():
            return None
        task_id, task = self.tasks.active()
        data = self.read(level, 'INVALID_EVIDENCE: damaged receipt or TDD log')
        receipt_task, receipt_baseline = normalized_receipt_binding(
            data, task_id, task.get('layout') == 'legacy'
        )
        wheel_current = 'wheel' not in data or (
            isinstance(data['wheel'], dict) and valid_wheel_result(level, data['wheel'])
        )
        data['current'] = (
            data.get('passed', True) is True
            and wheel_current
            and receipt_task == task_id
            and data.get('fingerprint') == fingerprint(self.project)
            and receipt_baseline == self.approval.validate_baseline()
        )
        return data
