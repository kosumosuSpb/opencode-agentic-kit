"""Task registry, layouts, and task lifecycle invariants."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .errors import GateError
from .project import state_dir
from .storage import StatefulService

TASK_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')


class TaskService(StatefulService):
    @property
    def registry_path(self) -> Path:
        return state_dir(self.project) / 'tasks.json'

    def _legacy_present(self) -> bool:
        external = state_dir(self.project)
        local = [
            self.project / '.agentic/drafts/spec.md',
            self.project / '.agentic/drafts/plan.md',
            self.project / 'specs/approved.md',
            self.project / 'plans/approved.md',
        ]
        return (external / 'baseline').exists() or any(path.exists() for path in local)

    def _new_registry(self, task_id: str, layout: str) -> dict[str, object]:
        return {
            'version': 2,
            'active_task': task_id,
            'tasks': {
                task_id: {
                    'created_at': self._clock().isoformat(timespec='seconds'),
                    'completed_at': None,
                    'layout': layout,
                }
            },
        }

    def _validate(self, data: object) -> dict[str, object]:
        if not isinstance(data, dict) or data.get('version') != 2:
            raise GateError('INVALID_TASK_REGISTRY: повреждён tasks.json')
        tasks = data.get('tasks')
        active = data.get('active_task')
        if not isinstance(tasks, dict) or (active is not None and active not in tasks):
            raise GateError('INVALID_TASK_REGISTRY: повреждён tasks.json')
        return data

    def load(self, *, create: bool = False) -> dict[str, object]:
        path = self.registry_path
        if path.is_file():
            try:
                return self._validate(json.loads(path.read_text()))
            except (ValueError, TypeError, AttributeError) as exc:
                raise GateError('INVALID_TASK_REGISTRY: повреждён tasks.json') from exc
        if path.exists() or path.is_symlink():
            raise GateError('INVALID_TASK_REGISTRY: tasks.json должен быть обычным файлом')
        if self._legacy_present():
            data = self._new_registry('legacy', 'legacy')
            self.storage.write(path, data)
            return data
        if not create:
            raise GateError('NO_ACTIVE_TASK: сначала /design или /task new <id>')
        data = self._new_registry('task-1', 'v2')
        self.storage.write(path, data)
        return data

    def active(self, *, create: bool = False) -> tuple[str, dict[str, object]]:
        data = self.load(create=create)
        task_id = data.get('active_task')
        tasks = data['tasks']
        if not isinstance(task_id, str) or not isinstance(tasks, dict) or task_id not in tasks:
            raise GateError('NO_ACTIVE_TASK: выбери задачу через /task use <id>')
        task = tasks[task_id]
        if not isinstance(task, dict):
            raise GateError('INVALID_TASK_REGISTRY: неверная запись задачи')
        return task_id, task

    def state_path(self) -> Path:
        task_id, task = self.active()
        if task.get('layout') == 'legacy':
            return state_dir(self.project)
        return state_dir(self.project) / 'tasks' / task_id

    def draft_directory(self, task_id: str, layout: str) -> Path:
        if layout == 'legacy':
            return self.project / '.agentic/drafts'
        return self.project / '.agentic/tasks' / task_id / 'drafts'

    def ensure_draft_directory(self, task_id: str, layout: str) -> Path:
        directory = self.draft_directory(task_id, layout)
        self.storage.reject_symlink_ancestors(directory / 'spec.md', self.project)
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if directory.is_symlink() or not directory.is_dir():
            raise GateError(f'INVALID_DRAFT_DIRECTORY: {directory}')
        return directory

    def create(self, task_id: str) -> str:
        if not TASK_ID.fullmatch(task_id):
            raise GateError('INVALID_TASK_ID: 1-64 ASCII letters, digits, dot, underscore or dash')
        try:
            data = self.load()
        except GateError as exc:
            if 'NO_ACTIVE_TASK' not in str(exc):
                raise
            data = {'version': 2, 'active_task': None, 'tasks': {}}
        tasks = data['tasks']
        assert isinstance(tasks, dict)
        if task_id in tasks:
            raise GateError(f'TASK_EXISTS: {task_id}')
        self.ensure_draft_directory(task_id, 'v2')
        tasks[task_id] = {
            'created_at': self._clock().isoformat(timespec='seconds'),
            'completed_at': None,
            'layout': 'v2',
        }
        data['active_task'] = task_id
        self.storage.write(self.registry_path, data)
        return f'ACTIVE TASK: {task_id} (new, unapproved)'

    def use(self, task_id: str) -> str:
        data = self.load()
        tasks = data['tasks']
        if not isinstance(tasks, dict) or task_id not in tasks:
            raise GateError(f'UNKNOWN_TASK: {task_id}')
        data['active_task'] = task_id
        self.storage.write(self.registry_path, data)
        return f'ACTIVE TASK: {task_id}'

    def info(self, *, create: bool = False) -> dict[str, object]:
        task_id, task = self.active(create=create)
        if create:
            self._prepare_info(task_id, task)
        drafts = self.draft_paths()
        working = self.working_paths()
        return self._info_record(task_id, task, drafts, working)

    def _prepare_info(self, task_id: str, task: dict[str, object]) -> None:
        if task.get('completed_at') is not None:
            raise GateError(f'TASK_COMPLETED: {task_id}; create the next task with /task new <id>')
        self.ensure_draft_directory(task_id, str(task.get('layout')))

    def _info_record(
        self,
        task_id: str,
        task: dict[str, object],
        drafts: tuple[Path, Path],
        working: tuple[Path, Path],
    ) -> dict[str, object]:
        return {
            'task_id': task_id,
            'completed_at': task.get('completed_at'),
            'draft_spec': str(drafts[0].relative_to(self.project)),
            'draft_plan': str(drafts[1].relative_to(self.project)),
            'approved_spec': str(working[0].relative_to(self.project)),
            'approved_plan': str(working[1].relative_to(self.project)),
        }

    def require_open(self) -> str:
        task_id, task = self.active()
        if task.get('completed_at') is not None:
            raise GateError(f'TASK_COMPLETED: {task_id}; create or select an open task')
        return task_id

    def baseline_paths(self) -> tuple[Path, Path]:
        local = self.state_path() / 'baseline'
        return local / 'spec.md', local / 'plan.md'

    def working_paths(self) -> tuple[Path, Path]:
        task_id, task = self.active()
        if task.get('layout') == 'legacy':
            return self.project / 'specs/approved.md', self.project / 'plans/approved.md'
        return (
            self.project / 'specs/tasks' / task_id / 'approved.md',
            self.project / 'plans/tasks' / task_id / 'approved.md',
        )

    def draft_paths(self) -> tuple[Path, Path]:
        task_id, task = self.active()
        local = self.draft_directory(task_id, str(task.get('layout')))
        return local / 'spec.md', local / 'plan.md'
