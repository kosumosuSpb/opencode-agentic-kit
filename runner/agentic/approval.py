"""Human approval, baseline validation, and reapproval audit."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .errors import GateError
from .project import file_digest, state_dir
from .storage import JsonStorage, StatefulService
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

    def baseline_paths(self) -> tuple[Path, Path]:
        return self.tasks.baseline_paths()

    def working_paths(self) -> tuple[Path, Path]:
        return self.tasks.working_paths()

    def draft_paths(self) -> tuple[Path, Path]:
        return self.tasks.draft_paths()

    def validate_baseline(self, *, require_approved_status: bool = True) -> dict[str, str]:
        task_id, _ = self.tasks.active()
        baseline = self.tasks.baseline_paths()
        working = self.tasks.working_paths()
        if not all(path.is_file() for path in baseline):
            raise GateError('NO_APPROVAL: нет утверждённого baseline; сначала /approve')
        if not all(path.is_file() for path in working):
            raise GateError('APPROVED_DOC_MISSING: рабочая спецификация или план отсутствует')
        expected = [file_digest(path) for path in baseline]
        actual = [file_digest(path) for path in working]
        if expected != actual:
            raise GateError('SPEC_CHANGED: утверждённая спецификация/план не совпадают с baseline')
        if require_approved_status:
            for path in working:
                validate_approved_document(path)
        return {'task_id': task_id, 'spec_sha256': expected[0], 'plan_sha256': expected[1]}

    def _evidence_paths(self) -> list[Path]:
        state = self.tasks.state_path()
        return [
            state / 'tdd.json',
            *(state / f'{level}.json' for level in ('fast', 'verify', 'full')),
        ]

    def archive(
        self,
        baseline: tuple[Path, Path],
        working: tuple[Path, Path],
        approval: dict[str, str],
    ) -> Path:
        stamp = self._clock().strftime('%Y%m%dT%H%M%S.%fZ')
        archive = (
            self.tasks.state_path() / 'approval-history' / f'{stamp}-{approval["spec_sha256"][:12]}'
        )
        archive.mkdir(parents=True, mode=0o700)
        names = ('baseline-spec.md', 'baseline-plan.md', 'approved-spec.md', 'approved-plan.md')
        for name, source in zip(names, (*baseline, *working)):
            (archive / name).write_bytes(source.read_bytes())
        copied = []
        for source in self._evidence_paths():
            if source.is_file():
                destination = archive / source.name
                destination.write_bytes(source.read_bytes())
                copied.append(destination.name)
        self.storage.write(
            archive / 'revision.json',
            {
                'superseded_at': self._clock().isoformat(timespec='seconds'),
                'approval': approval,
                'evidence_files': copied,
            },
        )
        return archive

    def _validate_inputs(self, replace_existing: bool) -> tuple[list[Path], list[bytes]]:
        task_id = self.tasks.require_open()
        baseline = self.tasks.baseline_paths()
        working = self.tasks.working_paths()
        self._validate_existing_paths(baseline, working, replace_existing)
        drafts = self._validate_drafts(task_id)
        self._validate_managed_paths(drafts, working, baseline)
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

    def _validate_drafts(self, task_id: str) -> list[Path]:
        drafts = list(self.tasks.draft_paths())
        if any(path.is_symlink() for path in drafts):
            raise GateError('SYMLINK_DRAFT: черновик не может быть символьной ссылкой')
        if not all(path.is_file() and path.stat().st_size for path in drafts):
            missing = ', '.join(str(path.relative_to(self.project)) for path in drafts)
            raise GateError(f'DRAFT_MISSING: нужны черновики активной задачи {task_id}: {missing}')
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

    def _replace(self, contents: list[bytes]) -> ApprovalOutcome:
        baseline = self.tasks.baseline_paths()
        working = self.tasks.working_paths()
        previous = self.validate_baseline(require_approved_status=False)
        archive = self.archive(baseline, working, previous)
        old = {path: path.read_bytes() for path in (*working, *baseline)}
        try:
            for source_bytes, destination, snapshot in zip(contents, working, baseline):
                for path in (destination, snapshot):
                    temporary = path.with_suffix(path.suffix + '.approval-new')
                    temporary.write_bytes(source_bytes)
                    os.replace(temporary, path)
        except Exception:
            for path, source_bytes in old.items():
                temporary = path.with_suffix(path.suffix + '.approval-restore')
                temporary.write_bytes(source_bytes)
                os.replace(temporary, path)
            raise
        return ApprovalOutcome('replaced', self.validate_baseline(), archive)

    def _create(self, contents: list[bytes]) -> ApprovalOutcome:
        baseline = self.tasks.baseline_paths()
        working = self.tasks.working_paths()
        created: list[Path] = []
        try:
            for source_bytes, destination, snapshot in zip(contents, working, baseline):
                for path in (destination, snapshot):
                    with path.open('xb') as stream:
                        created.append(path)
                        stream.write(source_bytes)
        except Exception:
            for path in reversed(created):
                path.unlink(missing_ok=True)
            raise
        return ApprovalOutcome('created', self.validate_baseline())

    def approve(self, *, replace_existing: bool = False) -> ApprovalOutcome:
        baseline = self.tasks.baseline_paths()
        if all(path.is_file() for path in baseline) and not replace_existing:
            return ApprovalOutcome('existing', self.validate_baseline())
        _, contents = self._validate_inputs(replace_existing)
        working = self.tasks.working_paths()
        for parent in {path.parent for path in [*working, *baseline]}:
            parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        return self._replace(contents) if replace_existing else self._create(contents)
