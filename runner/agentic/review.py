"""Independent review, acceptance, superseding, and reopen lifecycle."""

from __future__ import annotations

import json
import re
import secrets
import shutil
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .approval import ApprovalService
from .errors import GateError
from .project import file_digest, fingerprint, state_dir
from .storage import JsonStorage
from .tasks import TaskService

REVIEW_ID = re.compile(r'[0-9a-f]{32}')
FINDING = re.compile(r'(major|minor|process):([^\s]{1,240})')


def parse_findings(tokens: list[str]) -> list[dict[str, str]]:
    if tokens == ['none']:
        return []
    findings = []
    for token in tokens:
        match = FINDING.fullmatch(token)
        if not match:
            raise GateError(
                'INVALID_FINDING: use severity:concrete-location-or-reason without spaces'
            )
        findings.append({'severity': match.group(1), 'detail': match.group(2)})
    return findings


def same_review_subject(left: object, right: object) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return all(left.get(key) == right.get(key) for key in ('task_id', 'approval', 'fingerprint'))


class ReviewService:
    def __init__(
        self,
        project: Path,
        tasks: TaskService,
        approval: ApprovalService,
        storage: JsonStorage,
        receipt_reader: Callable[[str], dict[str, object]],
        receipt_path: Callable[[str], Path],
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self.project = project
        self.tasks = tasks
        self.approval = approval
        self.storage = storage
        self._receipt_reader = receipt_reader
        self._receipt_path = receipt_path
        self._clock = clock or (lambda: datetime.now(UTC))
        self._id_factory = id_factory or (lambda: secrets.token_hex(16))

    def review_path(self, review_id: str) -> Path:
        if not REVIEW_ID.fullmatch(review_id):
            raise GateError('INVALID_REVIEW_ID: expected the id printed by /finalize')
        return self.tasks.state_path() / 'reviews' / f'{review_id}.json'

    @property
    def current_path(self) -> Path:
        return self.tasks.state_path() / 'review-current.json'

    @property
    def current_verify_path(self) -> Path:
        return self.tasks.state_path() / 'review-verify-current.json'

    def binding(self, level: str = 'full') -> dict[str, object]:
        task_id, _ = self.tasks.active()
        receipt = self._receipt_reader(level)
        path = self._receipt_path(level)
        return {
            'task_id': task_id,
            'approval': self.approval.validate_baseline(),
            'fingerprint': fingerprint(self.project),
            f'{level}_receipt': str(path),
            f'{level}_receipt_sha256': file_digest(path),
            f'{level}_passed_at': receipt.get('passed_at'),
        }

    def _pointer_record(self, pointer_path: Path) -> dict[str, object] | None:
        if not pointer_path.is_file():
            return None
        pointer = self.storage.read_object(pointer_path, 'INVALID_CURRENT_REVIEW: damaged pointer')
        review_id = pointer.get('review_id')
        if not isinstance(review_id, str) or not REVIEW_ID.fullmatch(review_id):
            return None
        path = self.review_path(review_id)
        if not path.is_file():
            raise GateError('INVALID_CURRENT_REVIEW: review record is missing')
        return self.storage.read_object(path, 'INVALID_REVIEW: damaged review record')

    def _supersede_pending(self, pointer_path: Path, replacement: str) -> None:
        prior = self._pointer_record(pointer_path)
        if prior is None or prior.get('status') != 'pending':
            return
        self._mark_superseded(prior, replacement)

    def _mark_superseded(self, prior: dict[str, object], replacement: str) -> None:
        prior['status'] = 'superseded'
        prior['checks_completed'] = False
        prior['superseded_by'] = replacement
        prior['submitted_at'] = self._clock().isoformat(timespec='seconds')
        self.storage.write(self.review_path(str(prior['review_id'])), prior)

    def _current_independent_review(
        self, full_binding: dict[str, object]
    ) -> dict[str, object] | None:
        record = self._pointer_record(self.current_verify_path)
        if record is None or record.get('phase') != 'verify':
            return None
        if not same_review_subject(record.get('binding'), full_binding):
            if record.get('status') not in {'stale', 'superseded'}:
                record['previous_status'] = record.get('status')
                record['status'] = 'stale'
                record['checks_completed'] = False
                record['invalidated_at'] = self._clock().isoformat(timespec='seconds')
                record['invalidation_reason'] = (
                    'task, approval or fingerprint changed after /review'
                )
                self.storage.write(self.review_path(str(record['review_id'])), record)
            return None
        if record.get('status') == 'pending':
            raise GateError('VERIFY_REVIEW_PENDING: дождись заключения /review перед /finalize')
        if (
            record.get('status') not in {'passed', 'rejected'}
            or record.get('checks_completed') is not True
        ):
            return None
        return record

    def begin(self, phase: str = 'finalize') -> dict[str, object]:
        if phase not in {'verify', 'finalize'}:
            raise GateError(f'INVALID_REVIEW_PHASE: {phase}')
        self.tasks.require_open()
        level = 'verify' if phase == 'verify' else 'full'
        binding = self.binding(level)
        current = self.current_verify_path if phase == 'verify' else self.current_path
        existing, reusable = self._existing_review(current, phase, binding)
        if reusable is not None:
            return reusable
        review_id = self._id_factory()
        source = self._current_independent_review(binding) if phase == 'finalize' else None
        record = self._new_record(review_id, phase, binding, source)
        path = self.review_path(review_id)
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.storage.write(path, record)
        self.storage.write(current, {'review_id': review_id})
        if existing is not None and existing.get('status') == 'pending':
            self._mark_superseded(existing, review_id)
        return {'created': True, **record}

    def _existing_review(
        self, current: Path, phase: str, binding: dict[str, object]
    ) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        if not current.is_file():
            return None, None
        existing = self._pointer_record(current)
        if existing is None:
            return None, None
        reusable = all(
            (
                existing.get('phase') == phase,
                existing.get('binding') == binding,
                existing.get('status') in {'pending', 'passed'},
            )
        )
        return existing, {'created': False, **existing} if reusable else None

    def _new_record(
        self,
        review_id: str,
        phase: str,
        binding: dict[str, object],
        source: dict[str, object] | None,
    ) -> dict[str, object]:
        return {
            'version': 2,
            'review_id': review_id,
            'phase': phase,
            'mode': self._review_mode(phase, source),
            'status': 'pending',
            'binding': binding,
            'source_review_id': source.get('review_id') if source is not None else None,
            'carried_findings': source.get('findings', []) if source is not None else [],
            'started_at': self._clock().isoformat(timespec='seconds'),
        }

    @staticmethod
    def _review_mode(phase: str, source: dict[str, object] | None) -> str:
        if phase == 'verify':
            return 'independent-review'
        return 'reuse-independent-review' if source is not None else 'full-independent-review'

    def status(self) -> dict[str, object]:
        self.tasks.require_open()
        if not self.current_path.is_file():
            return {'current': False, 'status': 'none'}
        pointer = self.storage.read_object(
            self.current_path, 'INVALID_CURRENT_REVIEW: damaged pointer'
        )
        review_id = pointer.get('review_id')
        if not isinstance(review_id, str) or not REVIEW_ID.fullmatch(review_id):
            return {'current': False, 'status': 'none'}
        path = self.review_path(review_id)
        if not path.is_file():
            raise GateError('INVALID_CURRENT_REVIEW: review record is missing')
        record = self.storage.read_object(path, 'INVALID_REVIEW: damaged review record')
        try:
            current = record.get('phase') == 'finalize' and record.get('binding') == self.binding(
                'full'
            )
        except GateError:
            current = False
        return {'current': current, **record}

    @staticmethod
    def _validate_findings(verdict: str, findings: list[dict[str, str]], carried: object) -> None:
        ReviewService._validate_submitted_findings(verdict, findings)
        ReviewService._validate_carried_findings(verdict, carried)

    @staticmethod
    def _validate_submitted_findings(verdict: str, findings: list[dict[str, str]]) -> None:
        if verdict in {'reject', 'exception'} and not findings:
            raise GateError('FINDINGS_REQUIRED: reject/exception requires a concrete finding')
        if verdict == 'pass' and any(item['severity'] in {'major', 'process'} for item in findings):
            raise GateError('PASS_WITH_BLOCKING_FINDINGS: major/process findings cannot be PASS')

    @staticmethod
    def _validate_carried_findings(verdict: str, carried: object) -> None:
        if verdict != 'pass' or not isinstance(carried, list):
            return
        blockers = {item.get('severity') for item in carried if isinstance(item, dict)}
        if 'major' in blockers:
            raise GateError(
                'UNRESOLVED_MAJOR_FINDINGS: change the code and obtain a current review'
            )
        if 'process' in blockers:
            raise GateError(
                'UNRESOLVED_PROCESS_FINDINGS: resolve the process finding and obtain a current review'
            )

    def _stale_submission(
        self,
        path: Path,
        record: dict[str, object],
        verdict: str,
        reason: str,
    ) -> None:
        record.update(
            {
                'status': 'stale',
                'submitted_verdict': verdict,
                'checks_completed': False,
                'process_exception': reason,
                'submitted_at': self._clock().isoformat(timespec='seconds'),
            }
        )
        self.storage.write(path, record)

    def submit(
        self, review_id: str, verdict: str, finding_tokens: list[str] | None = None
    ) -> dict[str, object]:
        path = self.review_path(review_id)
        if not path.is_file():
            raise GateError(f'UNKNOWN_REVIEW: {review_id}')
        record = self.storage.read_object(path, 'INVALID_REVIEW: damaged review record')
        findings = parse_findings(finding_tokens or [])
        self._validate_findings(verdict, findings, record.get('carried_findings', []))
        status = {'pass': 'passed', 'reject': 'rejected', 'exception': 'exception'}[verdict]
        completed = self._prior_submission(review_id, record, status)
        if completed is not None:
            return completed
        phase = record.get('phase')
        self._require_current(review_id, phase)
        level = 'verify' if phase == 'verify' else 'full'
        self._submission_binding(path, record, verdict, level)
        self._complete_submission(path, record, verdict, status, findings)
        return {'already_submitted': False, **record}

    @staticmethod
    def _prior_submission(
        review_id: str, record: dict[str, object], status: str
    ) -> dict[str, object] | None:
        existing = record.get('status')
        if existing == 'pending':
            return None
        if existing == status:
            return {'already_submitted': True, **record}
        raise GateError(f'REVIEW_ALREADY_SUBMITTED: {review_id} is {existing}')

    def _require_current(self, review_id: str, phase: object) -> None:
        pointer_path = self.current_verify_path if phase == 'verify' else self.current_path
        pointer = self.storage.read_object(pointer_path, 'INVALID_CURRENT_REVIEW: damaged pointer')
        if pointer.get('review_id') != review_id:
            raise GateError('SUPERSEDED_REVIEW: only the current /finalize review may be submitted')

    def _submission_binding(
        self,
        path: Path,
        record: dict[str, object],
        verdict: str,
        level: str,
    ) -> dict[str, object]:
        try:
            binding = self.binding(level)
        except GateError as exc:
            self._stale_submission(path, record, verdict, str(exc))
            raise GateError(f'STALE_REVIEW: {exc}') from exc
        if record.get('binding') != binding:
            reason = 'review binding no longer matches current FULL receipt'
            self._stale_submission(path, record, verdict, reason)
            raise GateError('STALE_REVIEW: task, approval, fingerprint or FULL receipt changed')
        return binding

    def _complete_submission(
        self,
        path: Path,
        record: dict[str, object],
        verdict: str,
        status: str,
        findings: list[dict[str, str]],
    ) -> None:
        record.update(
            {
                'status': status,
                'submitted_verdict': verdict,
                'checks_completed': verdict in {'pass', 'reject'},
                'findings': findings,
                'submitted_at': self._clock().isoformat(timespec='seconds'),
            }
        )
        if verdict == 'exception':
            record['process_exception'] = 'Reviewer reported an incomplete review process'
        self.storage.write(path, record)

    def _current_final_review_id(self) -> str:
        if not self.current_path.is_file():
            raise GateError(
                'CURRENT_FINAL_REVIEW_REQUIRED: run /finalize and wait for Reviewer PASS'
            )
        record = self._pointer_record(self.current_path)
        if record is None:
            raise GateError('INVALID_CURRENT_REVIEW: damaged pointer')
        if record.get('phase') != 'finalize':
            raise GateError('CURRENT_FINAL_REVIEW_REQUIRED: current review is not /finalize')
        review_id = record.get('review_id')
        if not isinstance(review_id, str) or not REVIEW_ID.fullmatch(review_id):
            raise GateError('INVALID_REVIEW: final review has no valid id')
        return review_id

    def accept(self, review_id: str | None = None) -> dict[str, object]:
        review_id = review_id or self._current_final_review_id()
        task_id, task = self.tasks.active()
        if task.get('completed_at') is not None:
            return self._completed_acceptance(task_id, task, review_id)
        path = self.review_path(review_id)
        if not path.is_file():
            raise GateError(f'UNKNOWN_REVIEW: {review_id}')
        record = self.storage.read_object(path, 'INVALID_REVIEW: damaged review record')
        if record.get('phase') != 'finalize':
            raise GateError('REVIEW_NOT_FINAL: /accept requires the Review ID printed by /finalize')
        if record.get('status') != 'passed' or record.get('checks_completed') is not True:
            raise GateError(f'REVIEW_NOT_PASSED: {review_id} is {record.get("status")}')
        pointer = self.storage.read_object(
            self.current_path, 'INVALID_CURRENT_REVIEW: damaged pointer'
        )
        if pointer.get('review_id') != review_id:
            raise GateError('SUPERSEDED_REVIEW: human acceptance requires the current review')
        binding = self.binding('full')
        if record.get('binding') != binding:
            raise GateError('STALE_REVIEW: task, approval, fingerprint or FULL receipt changed')
        accepted_at = self._clock().isoformat(timespec='seconds')
        data = self.tasks.load()
        tasks = data['tasks']
        assert isinstance(tasks, dict) and isinstance(tasks[task_id], dict)
        acceptance = {'review_id': review_id, 'accepted_at': accepted_at, **binding}
        tasks[task_id]['completed_at'] = accepted_at
        tasks[task_id]['acceptance'] = acceptance
        self.storage.write(self.tasks.registry_path, data)
        return {'already_completed': False, 'task_id': task_id, **acceptance}

    @staticmethod
    def _completed_acceptance(
        task_id: str, task: dict[str, object], review_id: str
    ) -> dict[str, object]:
        acceptance = task.get('acceptance')
        if isinstance(acceptance, dict) and acceptance.get('review_id') == review_id:
            return {'already_completed': True, 'task_id': task_id, **acceptance}
        if not isinstance(acceptance, dict):
            raise GateError(
                'LEGACY_PREMATURE_COMPLETION: task has completed_at but no acceptance audit; '
                'use /task reopen <id>, never edit tasks.json manually'
            )
        raise GateError(f'TASK_COMPLETED_BY_DIFFERENT_REVIEW: {task_id}')

    def reopen(self, task_id: str) -> dict[str, object]:
        data = self.tasks.load()
        tasks = data['tasks']
        if (
            not isinstance(tasks, dict)
            or task_id not in tasks
            or not isinstance(tasks[task_id], dict)
        ):
            raise GateError(f'UNKNOWN_TASK: {task_id}')
        task = tasks[task_id]
        previous = task.get('completed_at')
        if previous is None:
            raise GateError(f'TASK_NOT_COMPLETED: {task_id}')
        history = task.get('status_history')
        if history is not None and not isinstance(history, list):
            raise GateError('INVALID_TASK_REGISTRY: status_history must be a list')
        reason = (
            'human-reopened-after-acceptance'
            if isinstance(task.get('acceptance'), dict)
            else 'premature-finalize-without-human-acceptance'
        )
        reopened_at = self._clock().isoformat(timespec='seconds')
        target_state = self._task_state_for(task_id, task)
        stamp = self._clock().strftime('%Y%m%dT%H%M%S.%fZ')
        archive = target_state / 'status-history' / f'{stamp}-reopen'
        archive.mkdir(parents=True, mode=0o700)
        copied = self._archive_evidence(target_state, archive)
        self.storage.write(
            archive / 'reopen.json',
            {
                'reopened_at': reopened_at,
                'reason': reason,
                'task_before': json.loads(json.dumps(task)),
                'evidence_files': copied,
            },
        )
        self._apply_reopen(data, task_id, task, previous, reason, reopened_at, archive)
        return {
            'task_id': task_id,
            'reopened_at': reopened_at,
            'previous_completed_at': previous,
            'archive': str(archive),
        }

    def _task_state_for(self, task_id: str, task: dict[str, object]) -> Path:
        if task.get('layout') == 'legacy':
            return state_dir(self.project)
        return state_dir(self.project) / 'tasks' / task_id

    @staticmethod
    def _archive_evidence(target_state: Path, archive: Path) -> list[str]:
        copied = []
        for name in (
            'tdd.json',
            'fast.json',
            'verify.json',
            'full.json',
            'review-current.json',
            'review-verify-current.json',
        ):
            source = target_state / name
            if source.is_file():
                shutil.copy2(source, archive / name)
                copied.append(name)
        reviews = target_state / 'reviews'
        if reviews.is_dir():
            shutil.copytree(reviews, archive / 'reviews')
            copied.append('reviews/')
        return copied

    def _apply_reopen(
        self,
        data: dict[str, object],
        task_id: str,
        task: dict[str, object],
        previous: object,
        reason: str,
        reopened_at: str,
        archive: Path,
    ) -> None:
        self.storage.write(
            self.current_path,
            {
                'review_id': None,
                'invalidated_at': reopened_at,
                'reason': 'task reopened; a new /finalize review is required',
            },
        )
        self.storage.write(
            self.current_verify_path,
            {
                'review_id': None,
                'invalidated_at': reopened_at,
                'reason': 'task reopened; historical /review cannot be reused',
            },
        )
        history = task.setdefault('status_history', [])
        if not isinstance(history, list):
            raise GateError('INVALID_TASK_REGISTRY: status_history must be a list')
        history.append(
            {
                'event': 'reopened',
                'at': reopened_at,
                'reason': reason,
                'previous_completed_at': previous,
                'previous_acceptance': task.get('acceptance'),
                'archive': str(archive),
            }
        )
        task['completed_at'] = None
        task.pop('acceptance', None)
        data['active_task'] = task_id
        self.storage.write(self.tasks.registry_path, data)
