"""Inter-process regressions for the TDD evidence read-modify-write cycle."""

from __future__ import annotations

import fcntl
import json
import multiprocessing
import os
import subprocess
import tempfile
import unittest
from contextlib import contextmanager
from multiprocessing.queues import Queue
from multiprocessing.synchronize import Event
from pathlib import Path
from unittest.mock import patch

from runner.agentic import approval as approval_module
from runner.agentic import tdd
from runner.agentic.approval import ApprovalService
from runner.agentic.storage import JsonStorage
from runner.agentic.tasks import TaskService


class FakeTasks:
    def __init__(self, state: Path) -> None:
        self.state = state

    def require_open(self) -> str:
        return 'task-a'

    def state_path(self) -> Path:
        return self.state


class FakeApproval:
    baseline = {'task_id': 'task-a', 'plan_sha256': 'plan'}

    def validate_baseline(self) -> dict[str, str]:
        return self.baseline


def record_red(
    state: str,
    label: str,
    cycle: str,
    ready: Event,
    release: Event,
    results: Queue,
) -> None:
    tdd.fingerprint = lambda _project: 'fingerprint'

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        ready.set()
        if not release.wait(10):
            raise RuntimeError('test synchronization timed out')
        return subprocess.CompletedProcess(command, 1, f'{cycle} expected failure\n')

    recorder = tdd.TDDRecorder(
        Path(state), FakeTasks(Path(state)), FakeApproval(), JsonStorage(), process_runner=run
    )
    try:
        recorder.record(
            cycle,
            'red',
            ['python3', '-m', 'unittest'],
            f'{cycle} is not implemented',
        )
    except BaseException as exc:  # child diagnostics must reach the parent
        results.put((label, type(exc).__name__, str(exc)))
    else:
        results.put((label, 'ok', ''))


def record_project_red(
    project: str,
    state: str,
    ready: Event,
    release: Event,
    results: Queue,
) -> None:
    os.environ['AGENTIC_STATE_DIR'] = state
    tdd.fingerprint = lambda _project: 'fingerprint'
    root = Path(project)
    storage = JsonStorage()
    tasks = TaskService(root, storage)
    approval = ApprovalService(root, tasks, storage)

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        ready.set()
        if not release.wait(10):
            raise RuntimeError('test synchronization timed out')
        return subprocess.CompletedProcess(command, 1, 'expected concurrent failure\n')

    recorder = tdd.TDDRecorder(root, tasks, approval, storage, process_runner=run)
    try:
        recorder.record(
            'concurrent',
            'red',
            ['python3', '-m', 'unittest'],
            'concurrent behavior is absent',
        )
    except BaseException as exc:
        results.put(('record', type(exc).__name__, str(exc)))
    else:
        results.put(('record', 'ok', ''))


def replace_project_approval(project: str, state: str, results: Queue) -> None:
    os.environ['AGENTIC_STATE_DIR'] = state
    root = Path(project)
    storage = JsonStorage()
    tasks = TaskService(root, storage)
    approval = ApprovalService(root, tasks, storage)
    try:
        approval.approve(replace_existing=True)
    except BaseException as exc:
        results.put(('replace', type(exc).__name__, str(exc)))
    else:
        results.put(('replace', 'ok', ''))


def replace_after_release(project: str, state: str, release: Event, results: Queue) -> None:
    if not release.wait(10):
        results.put(('replace', 'RuntimeError', 'test synchronization timed out'))
        return
    os.environ['AGENTIC_STATE_DIR'] = state
    root = Path(project)
    storage = JsonStorage()
    tasks = TaskService(root, storage)
    approval = ApprovalService(root, tasks, storage)
    try:
        outcome = approval.approve(replace_existing=True)
    except BaseException as exc:
        results.put(('replace', type(exc).__name__, str(exc)))
    else:
        results.put(('replace', 'ok', outcome.kind))


def hold_file_lock(path: str, ready: Event, release: Event) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        ready.set()
        if not release.wait(10):
            raise RuntimeError('test synchronization timed out')
    finally:
        os.close(descriptor)


def replace_with_lock_signal(
    project: str,
    state: str,
    lock_requested: Event,
    results: Queue,
) -> None:
    os.environ['AGENTIC_STATE_DIR'] = state
    root = Path(project)
    storage = JsonStorage()
    tasks = TaskService(root, storage)
    approval = ApprovalService(root, tasks, storage)
    original_lock = approval_module.exclusive_file_lock

    @contextmanager
    def signaled_lock(path: Path, error_code: str):
        lock_requested.set()
        with original_lock(path, error_code):
            yield

    try:
        with patch.object(approval_module, 'exclusive_file_lock', signaled_lock):
            approval.approve(replace_existing=True)
    except BaseException as exc:
        results.put(('replace', type(exc).__name__, str(exc)))
    else:
        results.put(('replace', 'ok', ''))


def lock_is_held(path: Path) -> bool:
    if not path.exists():
        return False
    descriptor = os.open(path, os.O_RDWR)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        return False
    finally:
        os.close(descriptor)


class TDDConcurrencyTests(unittest.TestCase):
    def test_task_switch_while_replace_waits_fails_without_cross_task_damage(self):
        context = multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project'
            root.mkdir()
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            state = str(Path(tmp) / 'state')
            with patch.dict(os.environ, {'AGENTIC_STATE_DIR': state}):
                storage = JsonStorage()
                tasks = TaskService(root, storage)
                approvals = ApprovalService(root, tasks, storage)
                snapshots: dict[str, dict[str, object]] = {}
                for task_id in ('task-a', 'task-b'):
                    tasks.create(task_id)
                    spec, plan = tasks.draft_paths()
                    spec.write_text(f'Spec {task_id}')
                    plan.write_text(f'Plan {task_id}')
                    approvals.approve()
                    baseline = approvals.validate_baseline()
                    storage.write(
                        tasks.state_path() / 'tdd.json',
                        {'baseline': baseline, 'cycles': {task_id: []}},
                    )
                    snapshots[task_id] = {
                        'documents': {
                            path: path.read_bytes()
                            for path in (*tasks.working_paths(), *tasks.baseline_paths())
                        },
                        'tdd': (tasks.state_path() / 'tdd.json').read_bytes(),
                        'state': tasks.state_path(),
                    }
                tasks.use('task-a')
                spec, plan = tasks.draft_paths()
                spec.write_text('Replacement A')
                plan.write_text('Replacement plan A')
                task_a_state = tasks.state_path()
                lock_ready = context.Event()
                lock_release = context.Event()
                lock_requested = context.Event()
                results = context.Queue()
                holder = context.Process(
                    target=hold_file_lock,
                    args=(str(task_a_state / 'tdd.lock'), lock_ready, lock_release),
                )
                replacer = context.Process(
                    target=replace_with_lock_signal,
                    args=(str(root), state, lock_requested, results),
                )
                holder.start()
                self.assertTrue(lock_ready.wait(5))
                replacer.start()
                self.assertTrue(lock_requested.wait(5))
                tasks.use('task-b')
                lock_release.set()
                holder.join(5)
                replacer.join(5)
                self.assertFalse(holder.is_alive())
                self.assertFalse(replacer.is_alive())
                outcome = results.get(timeout=1)
                self.assertEqual(outcome[0:2], ('replace', 'GateError'))
                self.assertIn('APPROVAL_TASK_CHANGED', outcome[2])
                for task_id in ('task-a', 'task-b'):
                    tasks.use(task_id)
                    snapshot = snapshots[task_id]
                    documents = snapshot['documents']
                    assert isinstance(documents, dict)
                    self.assertEqual(
                        {path: path.read_bytes() for path in documents}, documents
                    )
                    state_path = snapshot['state']
                    assert isinstance(state_path, Path)
                    self.assertEqual(
                        (state_path / 'tdd.json').read_bytes(), snapshot['tdd']
                    )
                    self.assertFalse((state_path / 'approval-history').exists())

    def test_concurrent_identical_replaces_create_one_revision(self):
        context = multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project'
            root.mkdir()
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            state = str(Path(tmp) / 'state')
            with patch.dict(os.environ, {'AGENTIC_STATE_DIR': state}):
                storage = JsonStorage()
                tasks = TaskService(root, storage)
                tasks.create('task-a')
                spec, plan = tasks.draft_paths()
                spec.write_text('Spec A')
                plan.write_text('Plan A')
                approval = ApprovalService(root, tasks, storage)
                approval.approve()
                storage.write(
                    tasks.state_path() / 'tdd.json',
                    {'baseline': approval.validate_baseline(), 'cycles': {'old': []}},
                )
                spec.write_text('Spec B')
                plan.write_text('Plan B')
                release = context.Event()
                results = context.Queue()
                workers = [
                    context.Process(
                        target=replace_after_release,
                        args=(str(root), state, release, results),
                    )
                    for _ in range(2)
                ]
                for worker in workers:
                    worker.start()
                release.set()
                for worker in workers:
                    worker.join(5)
                    self.assertFalse(worker.is_alive())
                outcomes = sorted(results.get(timeout=1) for _ in workers)
                self.assertEqual(
                    outcomes,
                    [('replace', 'ok', 'existing'), ('replace', 'ok', 'replaced')],
                )
                archives = list((tasks.state_path() / 'approval-history').iterdir())
                self.assertEqual(len(archives), 1)
                self.assertTrue((archives[0] / 'tdd.json').is_file())
                self.assertFalse((tasks.state_path() / 'tdd.json').exists())

    def test_replace_waits_for_record_and_archives_confirmed_evidence(self):
        context = multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'project'
            root.mkdir()
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            state = str(Path(tmp) / 'state')
            with patch.dict(os.environ, {'AGENTIC_STATE_DIR': state}):
                storage = JsonStorage()
                tasks = TaskService(root, storage)
                tasks.create('task-a')
                spec, plan = tasks.draft_paths()
                spec.write_text('Spec A')
                plan.write_text('Plan A')
                approval = ApprovalService(root, tasks, storage)
                approval.approve()
                spec.write_text('Spec B')
                plan.write_text('Plan B')

                ready = context.Event()
                release = context.Event()
                results = context.Queue()
                recorder = context.Process(
                    target=record_project_red,
                    args=(str(root), state, ready, release, results),
                )
                replacer = context.Process(
                    target=replace_project_approval,
                    args=(str(root), state, results),
                )
                recorder.start()
                self.assertTrue(ready.wait(5))
                replacer.start()
                replacer.join(0.2)
                self.assertTrue(replacer.is_alive(), 'replace bypassed the TDD lock')
                release.set()
                recorder.join(5)
                replacer.join(5)
                self.assertFalse(recorder.is_alive())
                self.assertFalse(replacer.is_alive())
                outcomes = sorted(results.get(timeout=1) for _ in range(2))
                self.assertEqual(outcomes, [('record', 'ok', ''), ('replace', 'ok', '')])
                archives = list((tasks.state_path() / 'approval-history').iterdir())
                self.assertEqual(len(archives), 1)
                historical = json.loads((archives[0] / 'tdd.json').read_text())
                self.assertEqual(set(historical['cycles']), {'concurrent'})
                self.assertFalse((tasks.state_path() / 'tdd.json').exists())

    def test_concurrent_processes_preserve_both_evidence_records(self):
        context = multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            first_ready = context.Event()
            second_ready = context.Event()
            first_release = context.Event()
            second_release = context.Event()
            results = context.Queue()
            first = context.Process(
                target=record_red,
                args=(tmp, 'first', 'first', first_ready, first_release, results),
            )
            second = context.Process(
                target=record_red,
                args=(tmp, 'second', 'second', second_ready, second_release, results),
            )
            first.start()
            self.assertTrue(first_ready.wait(5), 'first process never entered its test command')
            second.start()

            if lock_is_held(state / 'tdd.lock'):
                first_release.set()
                self.assertTrue(second_ready.wait(5), 'second process never acquired the lock')
                second_release.set()
            else:
                self.assertTrue(second_ready.wait(5), 'second process never reached the conflict')
                first_release.set()
                second_release.set()

            first.join(5)
            second.join(5)
            self.assertFalse(first.is_alive(), 'first process did not exit')
            self.assertFalse(second.is_alive(), 'second process did not exit')
            outcomes = sorted(results.get(timeout=1) for _ in range(2))
            self.assertEqual(outcomes, [('first', 'ok', ''), ('second', 'ok', '')])
            document = json.loads((state / 'tdd.json').read_text())
            self.assertEqual(set(document['cycles']), {'first', 'second'})

    def test_same_cycle_conflict_fails_without_losing_confirmed_evidence(self):
        context = multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            first_ready, second_ready = context.Event(), context.Event()
            first_release, second_release = context.Event(), context.Event()
            results = context.Queue()
            first = context.Process(
                target=record_red,
                args=(tmp, 'first', 'shared', first_ready, first_release, results),
            )
            second = context.Process(
                target=record_red,
                args=(tmp, 'second', 'shared', second_ready, second_release, results),
            )
            first.start()
            self.assertTrue(first_ready.wait(5))
            second.start()
            self.assertTrue(lock_is_held(state / 'tdd.lock'))
            first_release.set()
            first.join(5)
            second.join(5)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            outcomes = sorted(results.get(timeout=1) for _ in range(2))
            self.assertEqual(outcomes[0], ('first', 'ok', ''))
            self.assertEqual(outcomes[1][0:2], ('second', 'GateError'))
            self.assertIn('TDD_SCOPE_DUPLICATE', outcomes[1][2])
            entries = json.loads((state / 'tdd.json').read_text())['cycles']['shared']
            self.assertEqual(len(entries), 1)

    def test_process_crash_releases_lock_for_next_writer(self):
        context = multiprocessing.get_context('fork')
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            ready, release = context.Event(), context.Event()
            results = context.Queue()
            owner = context.Process(
                target=record_red,
                args=(tmp, 'owner', 'crashed', ready, release, results),
            )
            owner.start()
            self.assertTrue(ready.wait(5))
            self.assertTrue(lock_is_held(state / 'tdd.lock'))
            owner.terminate()
            owner.join(5)
            self.assertFalse(owner.is_alive())
            self.assertFalse(lock_is_held(state / 'tdd.lock'))

            next_ready, next_release = context.Event(), context.Event()
            successor = context.Process(
                target=record_red,
                args=(tmp, 'successor', 'after-crash', next_ready, next_release, results),
            )
            successor.start()
            self.assertTrue(next_ready.wait(5))
            next_release.set()
            successor.join(5)
            self.assertFalse(successor.is_alive())
            self.assertEqual(results.get(timeout=1), ('successor', 'ok', ''))
            document = json.loads((state / 'tdd.json').read_text())
            self.assertEqual(set(document['cycles']), {'after-crash'})

    def test_write_failure_preserves_evidence_and_releases_process_lock(self):
        class FailingStorage(JsonStorage):
            def write(self, path: Path, data: object) -> None:
                if path.name == 'tdd.json':
                    raise OSError('simulated write failure')
                super().write(path, data)

        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            existing = {
                'baseline': FakeApproval.baseline,
                'cycles': {'existing': [{'stage': 'red', 'scope': 'feature'}]},
            }
            (state / 'tdd.json').write_text(json.dumps(existing))

            def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                return subprocess.CompletedProcess(command, 1, 'expected failure\n')

            original_fingerprint = tdd.fingerprint
            tdd.fingerprint = lambda _project: 'fingerprint'
            self.addCleanup(setattr, tdd, 'fingerprint', original_fingerprint)
            failing = tdd.TDDRecorder(
                state, FakeTasks(state), FakeApproval(), FailingStorage(), process_runner=run
            )
            with self.assertRaisesRegex(OSError, 'simulated write failure'):
                failing.record(
                    'failed', 'red', ['python3', '-m', 'unittest'], 'expected failure'
                )
            self.assertEqual(json.loads((state / 'tdd.json').read_text()), existing)
            self.assertFalse(lock_is_held(state / 'tdd.lock'))

            succeeding = tdd.TDDRecorder(
                state, FakeTasks(state), FakeApproval(), JsonStorage(), process_runner=run
            )
            succeeding.record(
                'after-failure', 'red', ['python3', '-m', 'unittest'], 'expected failure'
            )
            document = json.loads((state / 'tdd.json').read_text())
            self.assertEqual(set(document['cycles']), {'existing', 'after-failure'})


if __name__ == '__main__':
    unittest.main()
