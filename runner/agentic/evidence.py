"""Evidence collection and reviewer-facing projections."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .approval import ApprovalService
from .errors import GateError
from .project import fingerprint
from .receipts import ReceiptService
from .storage import JsonStorage
from .tasks import TaskService

LEVELS = ('fast', 'verify', 'full')
STAGES = ('red', 'green', 'refactor')


def command_summary(entry: dict[str, object]) -> dict[str, object]:
    keys = (
        'kind',
        'cwd',
        'command',
        'exit_code',
        'path',
        'sha256',
        'output_sha256',
        'output_tail',
        'imports',
        'import_names',
        'source_root',
        'source_root_excluded',
        'target',
        'tests',
        'probe_output_sha256',
        'tool_file_count',
    )
    summary = {key: entry[key] for key in keys if key in entry}
    output = entry.get('output')
    if isinstance(output, str):
        summary['output_sha256'] = hashlib.sha256(output.encode()).hexdigest()
        summary['output_tail'] = output[-1000:]
    elif isinstance(summary.get('output_tail'), str):
        summary['output_tail'] = str(summary['output_tail'])[-1000:]
    return summary


def wheel_evidence(
    receipt: dict[str, object], summaries: list[dict[str, object]], project: Path
) -> dict[str, object]:
    steps = _wheel_steps(summaries, project)
    structured = _structured_wheel(receipt)
    return {
        'applicable': structured.get('applicable'),
        'status': structured.get('status'),
        'reason': structured.get('reason'),
        'verified': _wheel_verified(receipt, structured, steps),
        'artifact': steps['artifact'],
        'build': steps['build'],
        'isolated_install': steps['install'],
        'import_provenance': steps['provenance'],
        'isolated_test': steps['isolated_test'],
        'note': 'Wheel is built in a temporary directory; no wheel in the worktree is expected.',
    }


def _wheel_steps(
    summaries: list[dict[str, object]], project: Path
) -> dict[str, list[dict[str, object]]]:
    artifact = [entry for entry in summaries if entry.get('kind') == 'wheel_artifact']
    build = [entry for entry in summaries if _command_contains(entry, 'wheel', 'build')]
    install = [entry for entry in summaries if _install_command(entry)]
    provenance = [
        entry
        for entry in summaries
        if entry.get('kind') == 'wheel_import_provenance'
        and entry.get('source_root_excluded') is True
    ]
    isolated_test = [
        entry
        for entry in summaries
        if entry.get('kind') == 'wheel_public_api_smoke'
        and entry.get('source_root_excluded') is True
        and Path(str(entry.get('cwd', project))).resolve() != project.resolve()
    ]
    return {
        'artifact': artifact,
        'build': build,
        'install': install,
        'provenance': provenance,
        'isolated_test': isolated_test,
    }


def _wheel_verified(
    receipt: dict[str, object],
    structured: dict[str, object],
    steps: dict[str, list[dict[str, object]]],
) -> bool:
    required = ('artifact', 'build', 'install', 'provenance', 'isolated_test')
    return bool(
        structured.get('status') == 'verified'
        and receipt.get('wheel_verified')
        and all(steps[name] for name in required)
    )


def _command_contains(entry: dict[str, object], *needles: str) -> bool:
    command = entry.get('command')
    return isinstance(command, list) and any(needle in command for needle in needles)


def _install_command(entry: dict[str, object]) -> bool:
    command = entry.get('command')
    return isinstance(command, list) and 'install' in command and '--target' in command


def _structured_wheel(receipt: dict[str, object]) -> dict[str, object]:
    wheel = receipt.get('wheel')
    if isinstance(wheel, dict):
        return wheel
    verified = bool(receipt.get('wheel_verified'))
    return {
        'applicable': verified,
        'status': 'verified' if verified else 'not_run',
        'reason': 'legacy receipt without structured wheel result',
    }


def tdd_cycles(cycles: object) -> dict[str, object]:
    if not isinstance(cycles, dict):
        return {}
    return {
        str(cycle): _cycle_summary(entries if isinstance(entries, list) else [])
        for cycle, entries in cycles.items()
    }


def _cycle_summary(entries: list[object]) -> dict[str, object]:
    records = [entry for entry in entries if isinstance(entry, dict)]
    stages = list(dict.fromkeys(entry.get('stage') for entry in records))
    scopes = {
        stage: [entry.get('scope', 'feature') for entry in records if entry.get('stage') == stage]
        for stage in STAGES
    }
    return {
        'stages': stages,
        'scopes': scopes,
        'complete': stages == list(STAGES) and all('feature' in scopes[stage] for stage in STAGES),
        'regression_complete': all('regression' in scopes[stage] for stage in STAGES),
        'records': [_tdd_record(entry) for entry in records],
    }


def _tdd_record(entry: dict[str, object]) -> dict[str, object]:
    keys = (
        'stage',
        'scope',
        'expected_failure',
        'recorded_at',
        'fingerprint_before',
        'fingerprint_after',
    )
    return command_summary(entry) | {key: entry[key] for key in keys if key in entry}


class EvidenceService:
    def __init__(
        self,
        project: Path,
        tasks: TaskService,
        approval: ApprovalService,
        receipts: ReceiptService,
        storage: JsonStorage,
    ) -> None:
        self.project = project
        self.tasks = tasks
        self.approval = approval
        self.receipts = receipts
        self.storage = storage

    @property
    def tdd_path(self) -> Path:
        return self.tasks.state_path() / 'tdd.json'

    def collect(self) -> dict[str, object]:
        task_id, _ = self.tasks.active()
        current_fingerprint = fingerprint(self.project)
        current_baseline = self.approval.validate_baseline()
        receipts = {
            level: receipt
            for level in LEVELS
            if (receipt := self.receipts.annotated(level)) is not None
        }
        tdd = self._read_tdd()
        registry = self.tasks.load()
        working = self.tasks.working_paths()
        baseline = self.tasks.baseline_paths()
        return {
            'state_store': str(self.tasks.state_path()),
            'active_task': task_id,
            'tasks': registry['tasks'],
            'current_fingerprint': current_fingerprint,
            'current_baseline': current_baseline,
            'sources': {
                'approved_spec': str(working[0]),
                'approved_plan': str(working[1]),
                'baseline_spec': str(baseline[0]),
                'baseline_plan': str(baseline[1]),
                'tdd': str(self.tdd_path),
                'acceptance_checks': str(self.tasks.state_path() / 'acceptance-checks.json'),
                'receipts': {level: str(self.receipts.path(level)) for level in LEVELS},
            },
            'receipts': receipts,
            'tdd': tdd,
            'acceptance_checks': self._read_acceptance_checks(),
        }

    def _read_tdd(self) -> dict[str, object]:
        if not self.tdd_path.is_file():
            return {'cycles': {}}
        return self.storage.read_object(
            self.tdd_path, 'INVALID_EVIDENCE: damaged receipt or TDD log'
        )

    def _read_acceptance_checks(self) -> list[dict[str, object]]:
        path = self.tasks.state_path() / 'acceptance-checks.json'
        if not path.is_file():
            return []
        data = self.storage.read_object(path, 'INVALID_ACCEPTANCE_CHECKS')
        entries = data.get('entries')
        if data.get('version') != 1 or not isinstance(entries, list) or not all(
            isinstance(entry, dict) for entry in entries
        ):
            raise GateError('INVALID_ACCEPTANCE_CHECKS')
        return entries

    def _acceptance_check_evidence(self, data: dict[str, object]) -> dict[str, object]:
        raw = data['acceptance_checks']
        assert isinstance(raw, list)
        current = data['current_fingerprint']
        approved = data['current_baseline']
        latest: dict[tuple[str, str, str], int] = {}
        for i, entry in enumerate(raw):
            # Only the most recent run of a given AC at the current binding counts.
            key = (str(entry.get('criterion')), repr(entry.get('baseline')),
                   str(entry.get('fingerprint')))
            latest[key] = i
        records = []
        for i, entry in enumerate(raw):
            key = (str(entry.get('criterion')), repr(entry.get('baseline')),
                   str(entry.get('fingerprint')))
            valid = (
                entry.get('baseline') == approved
                and entry.get('fingerprint') == current
                and entry.get('exit_code') == 0
                and latest[key] == i
            )
            records.append({**entry, 'current_pass': valid})
        return {
            'source': str(self.tasks.state_path() / 'acceptance-checks.json'),
            'records': records,
            'note': ('Historical TDD remains immutable. Current AC checks independently '
                     'prove a specified command on the present approval/fingerprint.'),
        }

    def review_context(self, level: str) -> dict[str, object]:
        data = self.collect()
        receipt = self._current_evidence_receipt(data, level)
        commands = receipt.get('commands')
        if not isinstance(commands, list):
            raise GateError('INVALID_RECEIPT: commands missing')
        summaries = [command_summary(entry) for entry in commands if isinstance(entry, dict)]
        tdd = data['tdd']
        tdd_data = tdd if isinstance(tdd, dict) else {}
        sources = data['sources']
        assert isinstance(sources, dict) and isinstance(sources['receipts'], dict)
        return {
            'active_task': data['active_task'],
            'task_audit': self._task_audit(data),
            'approved_documents': sources,
            'approval': data['current_baseline'],
            'current_fingerprint': data['current_fingerprint'],
            'tdd': self._tdd_evidence(tdd_data, data, sources),
            'acceptance_checks': self._acceptance_check_evidence(data),
            'receipt': {
                'source': sources['receipts'][level],
                'level': level,
                'current': True,
                'fingerprint': receipt.get('fingerprint'),
                'baseline': receipt.get('baseline'),
                'passed_at': receipt.get('passed_at'),
                'commands': summaries,
            },
            'wheel': wheel_evidence(receipt, summaries, self.project),
        }

    @staticmethod
    def _current_evidence_receipt(data: dict[str, object], level: str) -> dict[str, object]:
        receipts = data.get('receipts')
        receipt = receipts.get(level) if isinstance(receipts, dict) else None
        if not isinstance(receipt, dict):
            raise GateError(f'NO_{level.upper()}_RECEIPT: сначала пройди соответствующий gate')
        if receipt.get('passed', True) is not True:
            raise GateError(f'FAILED_{level.upper()}_RECEIPT: gate did not pass')
        if receipt.get('current') is not True:
            raise GateError(
                f'STALE_{level.upper()}_RECEIPT: код или approval изменился после проверки'
            )
        return receipt

    def _task_audit(self, data: dict[str, object]) -> dict[str, object]:
        tasks = data.get('tasks')
        active = tasks.get(data['active_task']) if isinstance(tasks, dict) else None
        history = active.get('status_history', []) if isinstance(active, dict) else []
        return {
            'status_history': history,
            'reopen_archives': sorted(
                str(path) for path in (self.tasks.state_path() / 'status-history').glob('*-reopen')
            ),
            'note': 'Archived evidence is historical; current receipts and review bindings decide freshness.',
        }

    def _tdd_evidence(
        self,
        tdd: dict[str, object],
        data: dict[str, object],
        sources: dict[str, object],
    ) -> dict[str, object]:
        history = self.tasks.state_path() / 'approval-history'
        baseline = tdd.get('baseline')
        baseline_current = baseline == data['current_baseline'] or (
            baseline is None and not history.exists()
        )
        return {
            'source': sources['tdd'],
            'baseline': baseline,
            'baseline_current': baseline_current,
            'binding': _tdd_binding(baseline, history.exists()),
            'cycles': tdd_cycles(tdd.get('cycles', {})),
        }


def _tdd_binding(baseline: object, has_history: bool) -> str:
    if baseline is not None:
        return 'explicit'
    return 'superseded-approval-only' if has_history else 'implicit-pre-reapproval'
