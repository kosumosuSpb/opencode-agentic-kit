"""Human approval, baseline validation, and reapproval audit."""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .errors import GateError
from .project import file_digest, state_dir
from .storage import JsonStorage, StatefulService, exclusive_file_lock
from .tasks import TaskService

APPROVED_STATUS = '**Статус:** APPROVED — утверждено человеком'
PENDING_APPROVAL = re.compile(
    r'\bDRAFT\b|требу\w*\s+утверждени\w*|не\s+(?:явля\w+\s+)?утвержд\w*',
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ApprovalOutcome:
    kind: str
    baseline: dict[str, str]
    archive: Path | None = None


@dataclass(frozen=True)
class ApprovalTarget:
    task_id: str
    state: Path
    baseline: tuple[Path, Path]
    working: tuple[Path, Path]
    drafts: tuple[Path, Path]


def approved_document(source: bytes, label: str) -> bytes:
    try:
        text = source.decode('utf-8')
    except UnicodeDecodeError as exc:
        raise GateError(f'APPROVAL_UTF8_REQUIRED: {label}') from exc
    status = re.compile(r'^\*\*Статус:\*\*[^\n]*$', re.MULTILINE | re.IGNORECASE)
    if status.search(text):
        text = status.sub(APPROVED_STATUS, text, count=1)
    else:
        lines = text.splitlines(keepends=True)
        position = 1 if lines and lines[0].startswith('# ') else 0
        lines.insert(position, ('\n' if position else '') + APPROVED_STATUS + '\n')
        text = ''.join(lines)
    pending = PENDING_APPROVAL.search(text)
    if pending:
        line = text.count('\n', 0, pending.start()) + 1
        raise GateError(f'PENDING_APPROVAL_TEXT: {label}:{line}; исправь черновик до /approve')
    return text.encode('utf-8')


def validate_approved_document(path: Path) -> None:
    try:
        text = path.read_text()
    except UnicodeDecodeError as exc:
        raise GateError(f'APPROVAL_UTF8_REQUIRED: {path}') from exc
    if APPROVED_STATUS not in text.splitlines():
        raise GateError(f'APPROVAL_STATUS_INVALID: {path}; требуется новый human approval')
    pending = PENDING_APPROVAL.search(text)
    if pending:
        line = text.count('\n', 0, pending.start()) + 1
        raise GateError(f'PENDING_APPROVAL_TEXT: {path}:{line}; требуется новый human approval')


class ApprovalService(StatefulService):
    def __init__(
        self,
        project: Path,
        tasks: TaskService,
        storage: JsonStorage,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        super().__init__(project, storage, clock)
        self.tasks = tasks

    def _target(self) -> ApprovalTarget:
        task_id, task = self.tasks.active()
        layout = str(task.get('layout'))
        if layout == 'legacy':
            state = state_dir(self.project)
            working = self.project / 'specs/approved.md', self.project / 'plans/approved.md'
        else:
            state = state_dir(self.project) / 'tasks' / task_id
            working = (
                self.project / 'specs/tasks' / task_id / 'approved.md',
                self.project / 'plans/tasks' / task_id / 'approved.md',
            )
        drafts = self.tasks.draft_directory(task_id, layout)
        return ApprovalTarget(
            task_id=task_id,
            state=state,
            baseline=(state / 'baseline/spec.md', state / 'baseline/plan.md'),
            working=working,
            drafts=(drafts / 'spec.md', drafts / 'plan.md'),
        )

    def baseline_paths(self) -> tuple[Path, Path]:
        return self._target().baseline

    def working_paths(self) -> tuple[Path, Path]:
        return self._target().working

    def draft_paths(self) -> tuple[Path, Path]:
        return self._target().drafts

    def validate_baseline(self, *, require_approved_status: bool = True) -> dict[str, str]:
        return self._validate_target_baseline(self._target(), require_approved_status)

    @staticmethod
    def _validate_target_baseline(
        target: ApprovalTarget, require_approved_status: bool
    ) -> dict[str, str]:
        if not all(path.is_file() for path in target.baseline):
            raise GateError('NO_APPROVAL: нет утверждённого baseline; сначала /approve')
        if not all(path.is_file() for path in target.working):
            raise GateError('APPROVED_DOC_MISSING: рабочая спецификация или план отсутствует')
        expected = [file_digest(path) for path in target.baseline]
        actual = [file_digest(path) for path in target.working]
        if expected != actual:
            raise GateError('SPEC_CHANGED: утверждённая спецификация/план не совпадают с baseline')
        if require_approved_status:
            for path in target.working:
                validate_approved_document(path)
        return {
            'task_id': target.task_id,
            'spec_sha256': expected[0],
            'plan_sha256': expected[1],
        }

    @staticmethod
    def _evidence_paths(state: Path) -> list[Path]:
        return [
            state / 'tdd.json',
            *(state / f'{level}.json' for level in ('fast', 'verify', 'full')),
        ]

    def archive(
        self,
        target: ApprovalTarget,
        approval: dict[str, str],
    ) -> Path:
        stamp = self._clock().strftime('%Y%m%dT%H%M%S.%fZ')
        archive = (
            target.state / 'approval-history' / f'{stamp}-{approval["spec_sha256"][:12]}'
        )
        archive.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        staging = archive.with_name(f'.{archive.name}.approval-new')
        staging.mkdir(mode=0o700)
        try:
            names = (
                'baseline-spec.md',
                'baseline-plan.md',
                'approved-spec.md',
                'approved-plan.md',
            )
            for name, source in zip(names, (*target.baseline, *target.working)):
                (staging / name).write_bytes(source.read_bytes())
            copied = []
            for source in self._evidence_paths(target.state):
                if source.is_file():
                    destination = staging / source.name
                    destination.write_bytes(source.read_bytes())
                    copied.append(destination.name)
            self.storage.write(
                staging / 'revision.json',
                {
                    'superseded_at': self._clock().isoformat(timespec='seconds'),
                    'approval': approval,
                    'evidence_files': copied,
                },
            )
            os.replace(staging, archive)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return archive

    def _validate_inputs(
        self, target: ApprovalTarget, replace_existing: bool
    ) -> tuple[list[Path], list[bytes]]:
        self._require_current_target(target)
        self._validate_existing_paths(target.baseline, target.working, replace_existing)
        drafts = self._validate_drafts(target)
        self._validate_managed_paths(drafts, target.working, target.baseline)
        contents = [
            approved_document(source.read_bytes(), str(source.relative_to(self.project)))
            for source in drafts
        ]
        return drafts, contents

    @staticmethod
    def _validate_existing_paths(
        baseline: tuple[Path, Path],
        working: tuple[Path, Path],
        replace_existing: bool,
    ) -> None:
        if replace_existing:
            ApprovalService._validate_replace_paths(baseline, working)
            return
        ApprovalService._validate_new_paths(baseline, working)

    @staticmethod
    def _validate_new_paths(baseline: tuple[Path, Path], working: tuple[Path, Path]) -> None:
        if any(path.exists() or path.is_symlink() for path in baseline):
            raise GateError('PARTIAL_BASELINE: повреждённый/частичный baseline; отказ')
        if any(path.exists() or path.is_symlink() for path in working):
            raise GateError(
                'EXISTING_APPROVED_PATH: файлы specs/approved.md и plans/approved.md '
                'уже существуют; не перезаписываю'
            )

    @staticmethod
    def _validate_replace_paths(baseline: tuple[Path, Path], working: tuple[Path, Path]) -> None:
        if not all(path.is_file() and not path.is_symlink() for path in (*baseline, *working)):
            raise GateError(
                'REPLACE_REQUIRES_COMPLETE_APPROVAL: текущий approval должен быть целым'
            )

    def _validate_drafts(self, target: ApprovalTarget) -> list[Path]:
        drafts = list(target.drafts)
        if any(path.is_symlink() for path in drafts):
            raise GateError('SYMLINK_DRAFT: черновик не может быть символьной ссылкой')
        if not all(path.is_file() and path.stat().st_size for path in drafts):
            missing = ', '.join(str(path.relative_to(self.project)) for path in drafts)
            raise GateError(
                f'DRAFT_MISSING: нужны черновики активной задачи {target.task_id}: {missing}'
            )
        return drafts

    def _validate_managed_paths(
        self,
        drafts: list[Path],
        working: tuple[Path, Path],
        baseline: tuple[Path, Path],
    ) -> None:
        for path in [*drafts, *working]:
            self.storage.reject_symlink_ancestors(path, self.project)
        for path in baseline:
            self.storage.reject_symlink_ancestors(path, state_dir(self.project))

    def _require_current_target(self, target: ApprovalTarget) -> None:
        try:
            current_task = self.tasks.require_open()
            current_target = self._target()
        except GateError as exc:
            raise GateError('APPROVAL_TASK_CHANGED: active task changed during approval') from exc
        if current_task != target.task_id or current_target != target:
            raise GateError('APPROVAL_TASK_CHANGED: active task changed during approval')

    def _replace(self, target: ApprovalTarget, contents: list[bytes]) -> ApprovalOutcome:
        target.state.mkdir(parents=True, mode=0o700, exist_ok=True)
        with exclusive_file_lock(target.state / 'tdd.lock', 'TDD_LOCK_FAILED'):
            self._require_current_target(target)
            previous = self._validate_target_baseline(target, False)
            if all(
                source_bytes == destination.read_bytes()
                for source_bytes, destination in zip(contents, target.working)
            ):
                self._require_current_target(target)
                return ApprovalOutcome('existing', previous)
            old = {
                path: path.read_bytes() for path in (*target.working, *target.baseline)
            }
            archive = self.archive(target, previous)
            tdd_path = target.state / 'tdd.json'
            try:
                for source_bytes, destination, snapshot in zip(
                    contents, target.working, target.baseline
                ):
                    for path in (destination, snapshot):
                        temporary = path.with_suffix(path.suffix + '.approval-new')
                        temporary.write_bytes(source_bytes)
                        os.replace(temporary, path)
                if tdd_path.is_file():
                    os.replace(tdd_path, archive / tdd_path.name)
                self._require_current_target(target)
                current = self._validate_target_baseline(target, True)
            except Exception:
                self._restore_replaced_approval(old, tdd_path, archive)
                raise
            return ApprovalOutcome('replaced', current, archive)

    @staticmethod
    def _restore_replaced_approval(
        old: dict[Path, bytes], tdd_path: Path, archive: Path
    ) -> None:
        archived_tdd = archive / tdd_path.name
        if not tdd_path.exists() and archived_tdd.is_file():
            os.replace(archived_tdd, tdd_path)
        for path, source_bytes in old.items():
            temporary = path.with_suffix(path.suffix + '.approval-restore')
            temporary.write_bytes(source_bytes)
            os.replace(temporary, path)
        shutil.rmtree(archive)

    def _create(self, target: ApprovalTarget, contents: list[bytes]) -> ApprovalOutcome:
        created: list[Path] = []
        try:
            self._require_current_target(target)
            for source_bytes, destination, snapshot in zip(
                contents, target.working, target.baseline
            ):
                for path in (destination, snapshot):
                    with path.open('xb') as stream:
                        created.append(path)
                        stream.write(source_bytes)
        except Exception:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise
        return ApprovalOutcome('created', self._validate_target_baseline(target, True))

    def approve(self, *, replace_existing: bool = False) -> ApprovalOutcome:
        target = self._target()
        if all(path.is_file() for path in target.baseline) and not replace_existing:
            return ApprovalOutcome('existing', self._validate_target_baseline(target, True))
        _, contents = self._validate_inputs(target, replace_existing)
        for parent in {path.parent for path in [*target.working, *target.baseline]}:
            parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        return (
            self._replace(target, contents)
            if replace_existing
            else self._create(target, contents)
        )
