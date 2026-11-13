#!/usr/bin/env python3
"""Deterministic checks and workflow state for OpenCode/Codex. Python 3.11+."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone

ROOT_GENERATED_DIRS = {
    '.git', '.venv', '.agentic', '.metrics', 'target', 'build', '.mypy_cache',
    '.pytest_cache', '.ruff_cache', 'node_modules',
}
COMMAND_LOG: list[dict[str, object]] = []
APPROVED_STATUS = '**Статус:** APPROVED — утверждено человеком'
PENDING_APPROVAL = re.compile(
    r'\bDRAFT\b|требу\w*\s+утверждени\w*|не\s+(?:явля\w+\s+)?утвержд\w*',
    re.IGNORECASE,
)


class GateError(Exception):
    """Blocked: cannot establish a valid quality result."""


class GateFailure(GateError):
    """A check ran and found defects."""


def identity(project: Path) -> str:
    return hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:20]


def validate_git_root(project: Path) -> None:
    try:
        result = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=project,
                                capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GateError('NOT_GIT_REPOSITORY: требуется git init') from exc
    if Path(result.stdout.strip()).resolve() != project.resolve():
        raise GateError('PROJECT_NOT_GIT_ROOT: укажи git top-level, а не подкаталог')


def state_dir(project: Path) -> Path:
    root = Path(os.environ.get('AGENTIC_STATE_DIR', str(Path.home() / '.local/state/opencode-agentic')))
    return root / 'projects' / identity(project)


TASK_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')


def registry_path(project: Path) -> Path:
    return state_dir(project) / 'tasks.json'


def _write_json_atomic(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(temp, path)


def _legacy_present(project: Path) -> bool:
    external = state_dir(project)
    local = [project / '.agentic/drafts/spec.md', project / '.agentic/drafts/plan.md',
             project / 'specs/approved.md', project / 'plans/approved.md']
    return (external / 'baseline').exists() or any(path.exists() for path in local)


def load_registry(project: Path, *, create: bool = False) -> dict[str, object]:
    """Load v2 task metadata, adopting a pre-v1.2 project as the legacy task."""
    path = registry_path(project)
    if path.is_file():
        try:
            data = json.loads(path.read_text())
            if data.get('version') != 2 or not isinstance(data.get('tasks'), dict):
                raise ValueError
            active = data.get('active_task')
            if active is not None and active not in data['tasks']:
                raise ValueError
            return data
        except (ValueError, TypeError, AttributeError) as exc:
            raise GateError('INVALID_TASK_REGISTRY: повреждён tasks.json') from exc
    if path.exists() or path.is_symlink():
        raise GateError('INVALID_TASK_REGISTRY: tasks.json должен быть обычным файлом')
    if _legacy_present(project):
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        data = {'version': 2, 'active_task': 'legacy', 'tasks': {
            'legacy': {'created_at': now, 'completed_at': None, 'layout': 'legacy'},
        }}
        _write_json_atomic(path, data)
        return data
    if not create:
        raise GateError('NO_ACTIVE_TASK: сначала /design или /task new <id>')
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    data = {'version': 2, 'active_task': 'task-1', 'tasks': {
        'task-1': {'created_at': now, 'completed_at': None, 'layout': 'v2'},
    }}
    _write_json_atomic(path, data)
    return data


def active_task(project: Path, *, create: bool = False) -> tuple[str, dict[str, object]]:
    data = load_registry(project, create=create)
    task_id = data.get('active_task')
    tasks = data['tasks']
    if not isinstance(task_id, str) or not isinstance(tasks, dict) or task_id not in tasks:
        raise GateError('NO_ACTIVE_TASK: выбери задачу через /task use <id>')
    task = tasks[task_id]
    if not isinstance(task, dict):
        raise GateError('INVALID_TASK_REGISTRY: неверная запись задачи')
    return task_id, task


def task_state_dir(project: Path) -> Path:
    task_id, task = active_task(project)
    if task.get('layout') == 'legacy':
        return state_dir(project)
    return state_dir(project) / 'tasks' / task_id


def task_draft_dir(project: Path, task_id: str, layout: str) -> Path:
    if layout == 'legacy':
        return project / '.agentic/drafts'
    return project / '.agentic/tasks' / task_id / 'drafts'


def ensure_task_draft_directory(project: Path, task_id: str, layout: str) -> Path:
    """Create only the active task's draft directory, rejecting symlink escapes."""
    directory = task_draft_dir(project, task_id, layout)
    reject_symlink_ancestors(directory / 'spec.md', project)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if directory.is_symlink() or not directory.is_dir():
        raise GateError(f'INVALID_DRAFT_DIRECTORY: {directory}')
    return directory


def task_new(project: Path, task_id: str) -> None:
    if not TASK_ID.fullmatch(task_id):
        raise GateError('INVALID_TASK_ID: 1-64 ASCII letters, digits, dot, underscore or dash')
    try:
        data = load_registry(project)
    except GateError as exc:
        if 'NO_ACTIVE_TASK' not in str(exc):
            raise
        data = {'version': 2, 'active_task': None, 'tasks': {}}
    tasks = data['tasks']
    assert isinstance(tasks, dict)
    if task_id in tasks:
        raise GateError(f'TASK_EXISTS: {task_id}')
    ensure_task_draft_directory(project, task_id, 'v2')
    tasks[task_id] = {
        'created_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'completed_at': None,
        'layout': 'v2',
    }
    data['active_task'] = task_id
    _write_json_atomic(registry_path(project), data)
    print(f'ACTIVE TASK: {task_id} (new, unapproved)')


def task_use(project: Path, task_id: str) -> None:
    data = load_registry(project)
    tasks = data['tasks']
    if not isinstance(tasks, dict) or task_id not in tasks:
        raise GateError(f'UNKNOWN_TASK: {task_id}')
    data['active_task'] = task_id
    _write_json_atomic(registry_path(project), data)
    print(f'ACTIVE TASK: {task_id}')


def task_status(project: Path) -> None:
    data = load_registry(project)
    print(json.dumps(data, indent=2))


def task_info(project: Path, *, create: bool = False) -> None:
    task_id, task = active_task(project, create=create)
    if create and task.get('completed_at') is not None:
        raise GateError(f'TASK_COMPLETED: {task_id}; create the next task with /task new <id>')
    if create:
        ensure_task_draft_directory(project, task_id, str(task.get('layout')))
    drafts = draft_paths(project)
    working = working_paths(project)
    print(json.dumps({
        'task_id': task_id,
        'completed_at': task.get('completed_at'),
        'draft_spec': str(drafts[0].relative_to(project)),
        'draft_plan': str(drafts[1].relative_to(project)),
        'approved_spec': str(working[0].relative_to(project)),
        'approved_plan': str(working[1].relative_to(project)),
    }))


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
            result = subprocess.run(command, cwd=project, capture_output=True, text=True,
                                    check=False, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {'command': command, 'available': False, 'output': str(exc)}
        return {'command': command, 'available': result.returncode == 0,
                'output': (result.stdout or result.stderr).strip()}

    if python[:3] == ['uv', 'run', '--locked']:
        # Context collection must not create/sync an environment merely to print versions.
        python_command = [*python, '--no-sync', 'python', '--version']
        pytest_command = [*backend, '--no-sync', 'pytest', '--version']
    else:
        python_command = [python[0], '--version']
        pytest_command = [*backend, 'pytest', '--version']
    test_command = [*backend, 'pytest', '-q']
    print(json.dumps({
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
    }, indent=2))


def require_open_task(project: Path) -> str:
    task_id, task = active_task(project)
    if task.get('completed_at') is not None:
        raise GateError(f'TASK_COMPLETED: {task_id}; create or select an open task')
    return task_id


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def reject_symlink_ancestors(path: Path, root: Path) -> None:
    """Reject every existing directory symlink between a trusted root and a file."""
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise GateError(f'PATH_OUTSIDE_ROOT: {path}') from exc
    current = root
    if current.is_symlink():
        raise GateError(f'SYMLINK_DIRECTORY: {current}')
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise GateError(f'SYMLINK_DIRECTORY: {current}')


def approved_paths(project: Path) -> tuple[Path, Path]:
    local = task_state_dir(project) / 'baseline'
    return local / 'spec.md', local / 'plan.md'


def working_paths(project: Path) -> tuple[Path, Path]:
    task_id, task = active_task(project)
    if task.get('layout') == 'legacy':
        return project / 'specs/approved.md', project / 'plans/approved.md'
    return (project / 'specs/tasks' / task_id / 'approved.md',
            project / 'plans/tasks' / task_id / 'approved.md')


def draft_paths(project: Path) -> tuple[Path, Path]:
    task_id, task = active_task(project)
    local = task_draft_dir(project, task_id, str(task.get('layout')))
    return local / 'spec.md', local / 'plan.md'


def approved_document(source: bytes, label: str) -> bytes:
    """Create an approved copy without mutating the historical draft."""
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


def validate_baseline(project: Path, *, require_approved_status: bool = True) -> dict[str, str]:
    task_id, _ = active_task(project)
    baseline = approved_paths(project)
    working = working_paths(project)
    if not all(f.is_file() for f in baseline):
        raise GateError('NO_APPROVAL: нет утверждённого baseline; сначала /approve')
    if not all(f.is_file() for f in working):
        raise GateError('APPROVED_DOC_MISSING: рабочая спецификация или план отсутствует')
    expected = [sha(f) for f in baseline]
    actual = [sha(f) for f in working]
    if expected != actual:
        raise GateError('SPEC_CHANGED: утверждённая спецификация/план не совпадают с baseline')
    if require_approved_status:
        for path in working:
            validate_approved_document(path)
    return {'task_id': task_id, 'spec_sha256': expected[0], 'plan_sha256': expected[1]}


def archive_approval(project: Path, baseline: tuple[Path, Path], working: tuple[Path, Path],
                     approval: dict[str, str]) -> Path:
    """Preserve the exact superseded approval and evidence before replacement."""
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    archive = task_state_dir(project) / 'approval-history' / (
        f'{stamp}-{approval["spec_sha256"][:12]}')
    archive.mkdir(parents=True, mode=0o700)
    for name, source in zip(
            ('baseline-spec.md', 'baseline-plan.md', 'approved-spec.md', 'approved-plan.md'),
            (*baseline, *working)):
        (archive / name).write_bytes(source.read_bytes())
    evidence_files = [tdd_path(project), *(receipt_path(project, level)
                                             for level in ('fast', 'verify', 'full'))]
    copied = []
    for source in evidence_files:
        if source.is_file():
            destination = archive / source.name
            destination.write_bytes(source.read_bytes())
            copied.append(destination.name)
    _write_json_atomic(archive / 'revision.json', {
        'superseded_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'approval': approval,
        'evidence_files': copied,
    })
    return archive


def approve(project: Path, *, replace_existing: bool = False) -> None:
    task_id = require_open_task(project)
    baseline = approved_paths(project)
    working = working_paths(project)
    if all(path.is_file() for path in baseline) and not replace_existing:
        print('APPROVED: уже существует baseline, проверяем неизменность')
        print(json.dumps(validate_baseline(project), indent=2))
        return
    if any(path.exists() or path.is_symlink() for path in baseline) and not replace_existing:
        raise GateError('PARTIAL_BASELINE: повреждённый/частичный baseline; отказ')
    if any(f.exists() or f.is_symlink() for f in working) and not replace_existing:
        raise GateError('EXISTING_APPROVED_PATH: файлы specs/approved.md и plans/approved.md уже существуют; не перезаписываю')
    if replace_existing and not all(path.is_file() and not path.is_symlink()
                                    for path in (*baseline, *working)):
        raise GateError('REPLACE_REQUIRES_COMPLETE_APPROVAL: текущий approval должен быть целым')
    drafts = list(draft_paths(project))
    if any(f.is_symlink() for f in drafts):
        raise GateError('SYMLINK_DRAFT: черновик не может быть символьной ссылкой')
    if not all(f.is_file() and f.stat().st_size for f in drafts):
        raise GateError(f'DRAFT_MISSING: нужны черновики активной задачи {task_id}: '
                        + ', '.join(str(path.relative_to(project)) for path in drafts))
    # Reject directory symlinks at every depth; otherwise writes could escape.
    for path in [*drafts, *working]:
        reject_symlink_ancestors(path, project)
    for path in baseline:
        reject_symlink_ancestors(path, state_dir(project))
    # Collect inputs and create every parent before the first output file.
    contents = [approved_document(source.read_bytes(), str(source.relative_to(project)))
                for source in drafts]
    for parent in {f.parent for f in [*working, *baseline]}:
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if replace_existing:
        previous = validate_baseline(project, require_approved_status=False)
        archive = archive_approval(project, baseline, working, previous)
        old = {path: path.read_bytes() for path in (*working, *baseline)}
        try:
            for source_bytes, dest, base in zip(contents, working, baseline):
                for path in (dest, base):
                    temp = path.with_suffix(path.suffix + '.approval-new')
                    temp.write_bytes(source_bytes)
                    os.replace(temp, path)
        except Exception:
            for path, source_bytes in old.items():
                temp = path.with_suffix(path.suffix + '.approval-restore')
                temp.write_bytes(source_bytes)
                os.replace(temp, path)
            raise
        print(f'APPROVAL REPLACED BY HUMAN: previous approval archived at {archive}')
        print(json.dumps(validate_baseline(project), indent=2))
        return
    created: list[Path] = []
    try:
        for source_bytes, dest, base in zip(contents, working, baseline):
            for path in [dest, base]:
                with path.open('xb') as fh:
                    created.append(path)
                    fh.write(source_bytes)
    except Exception:
        # Failure must not leave a partial approval that blocks retries.
        for path in reversed(created):
            path.unlink(missing_ok=True)
        raise
    # Soft approval: owner can modify baseline. Do not present this as OS-enforced.
    print('APPROVED (SOFT LOCK): создан snapshot; для жёсткой OS-изоляции нужен отдельный аккаунт')
    print(json.dumps(validate_baseline(project), indent=2))


def fingerprint(project: Path) -> str:
    """Digest tracked and untracked non-ignored files, including deletes and file names."""
    proc = subprocess.run(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=project, capture_output=True, check=True)
    paths = sorted(set(filter(None, proc.stdout.split(b'\0'))))
    h = hashlib.sha256()
    for raw in paths:
        relative = os.fsdecode(raw)
        # Avoid transient tool output; exclude only known generated directories.
        parts = Path(relative).parts
        generated_egg_info = (parts[0].endswith('.egg-info')
                              or (len(parts) > 1 and parts[0] == 'src'
                                  and parts[1].endswith('.egg-info')))
        if (parts[0] in ROOT_GENERATED_DIRS
                or '__pycache__' in parts
                or relative == '.coverage'
                or generated_egg_info):
            continue
        path = project / relative
        h.update(raw + b'\0')
        if path.is_symlink():
            h.update(b'LINK\0' + os.fsencode(os.readlink(path)))
        elif path.is_file():
            h.update(b'FILE\0' + sha(path).encode())
        elif not path.exists():
            h.update(b'DELETED')
        else:
            h.update(b'OTHER')
        h.update(b'\0')
    return h.hexdigest()


def run(project: Path, command: list[str], *, capture: bool = False,
        env: dict[str, str] | None = None, echo_capture: bool = True) -> str:
    print(f'$ (cd {shlex.quote(str(project))} && {shlex.join(command)})', flush=True)
    try:
        done = subprocess.run(command, cwd=project, text=True, env=env,
                              stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.STDOUT if capture else None, check=False)
    except FileNotFoundError as e:
        raise GateError(f'MISSING_EXECUTABLE: {command[0]}') from e
    if capture and echo_capture:
        print(done.stdout, end='', flush=True)
    COMMAND_LOG.append({'cwd': str(project), 'command': command, 'exit_code': done.returncode,
                        **({'output': done.stdout[-4000:]} if capture else {})})
    if done.returncode:
        raise GateFailure(f'COMMAND_FAILED(exit={done.returncode}): {" ".join(command)}')
    return done.stdout if capture else ''


def _python_invocation(python: list[str], project: Path, *args: str) -> list[str]:
    if python[:3] == ['uv', 'run', '--locked']:
        return ['uv', 'run', '--project', str(project), '--locked', 'python', *args]
    return [python[0], *args]


def _wheel_import_names(wheel: Path) -> list[str]:
    """Discover importable top-level names from the built artifact itself."""
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            declared = [name for name in names if name.endswith('.dist-info/top_level.txt')]
            candidates: set[str] = set()
            for name in declared:
                candidates.update(line.strip() for line in
                                  archive.read(name).decode('utf-8').splitlines())
            if not candidates:
                for name in names:
                    parts = name.split('/')
                    if not parts or parts[0].endswith(('.dist-info', '.data')):
                        continue
                    top = parts[0][:-3] if parts[0].endswith('.py') else parts[0]
                    if top.isidentifier():
                        candidates.add(top)
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise GateError('INVALID_WHEEL: cannot inspect built artifact') from exc
    imports = sorted(name for name in candidates if name.isidentifier())
    if not imports:
        raise GateError('WHEEL_NO_IMPORTABLE_MODULES: artifact exposes no Python API')
    return imports


def _annotate_last_command(start: int, **metadata: object) -> None:
    """Attach structured evidence when run() recorded the command (unit fakes may not)."""
    if len(COMMAND_LOG) > start:
        COMMAND_LOG[-1].update(metadata)


def _isolated_wheel_checks(project: Path, python: list[str], scratch: Path,
                           site: Path, wheel: Path) -> None:
    """Import the artifact and run project-owned tests without a source-tree path."""
    imports = _wheel_import_names(wheel)
    provenance = r'''
import importlib, json, pathlib, sys
site = pathlib.Path(sys.argv[1]).resolve()
source = pathlib.Path(sys.argv[2]).resolve()
names = json.loads(sys.argv[3])
kept = []
for entry in sys.path:
    if not entry:
        continue
    resolved = pathlib.Path(entry).resolve()
    if resolved == source or source in resolved.parents:
        continue
    kept.append(entry)
sys.path[:] = [str(site), *kept]
result = {}
for name in names:
    module = importlib.import_module(name)
    origins = []
    filename = getattr(module, '__file__', None)
    if filename:
        origins.append(pathlib.Path(filename).resolve())
    module_path = getattr(module, '__path__', None)
    if module_path:
        origins.extend(pathlib.Path(item).resolve() for item in module_path)
    if not origins or any(origin != site and site not in origin.parents for origin in origins):
        raise RuntimeError(f'{name} imported outside wheel target: {origins}')
    result[name] = [str(origin) for origin in origins]
print(json.dumps({'imports': result, 'source_root_excluded': True}, sort_keys=True))
'''.strip()
    isolated_env = {key: value for key, value in os.environ.items()
                    if key not in {'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP'} }
    isolated_env['PYTHONNOUSERSITE'] = '1'
    before = len(COMMAND_LOG)
    run(scratch, _python_invocation(python, project, '-I', '-S', '-c', provenance,
                                    str(site), str(project), json.dumps(imports)),
        env=isolated_env, capture=True)
    _annotate_last_command(before, kind='wheel_import_provenance', imports=imports,
                           source_root=str(project), source_root_excluded=True,
                           target=str(site))

    tests = project / 'tests'
    for candidate in tests.rglob('*'):
        if candidate.is_symlink():
            raise GateError(f'WHEEL_SMOKE_SYMLINK: tests cannot escape source tree: {candidate}')
    smoke_tests = scratch / 'tests'
    shutil.copytree(tests, smoke_tests)
    shutil.copy2(project / 'pyproject.toml', scratch / 'pyproject.toml')

    probe = r'''
import hashlib, importlib.metadata as metadata, json, pathlib, re, sysconfig
from packaging.requirements import Requirement
purelib = pathlib.Path(sysconfig.get_paths()['purelib']).resolve()
queue = ['pytest']
seen = set()
files = set()
while queue:
    name = queue.pop()
    key = re.sub(r'[-_.]+', '-', name).lower()
    if key in seen:
        continue
    seen.add(key)
    try:
        distribution = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        continue
    for item in distribution.files or ():
        source = pathlib.Path(distribution.locate_file(item)).resolve()
        if source.is_file() and (source == purelib or purelib in source.parents):
            files.add(str(source.relative_to(purelib)))
    for raw_requirement in distribution.requires or ():
        requirement = Requirement(raw_requirement)
        if requirement.marker and not requirement.marker.evaluate({'extra': ''}):
            continue
        queue.append(requirement.name)
print(json.dumps({'purelib': str(purelib), 'tool_files': sorted(files)}))
'''.strip()
    probe_start = len(COMMAND_LOG)
    tool_paths_raw = run(scratch, _python_invocation(python, project, '-I', '-c', probe),
                         env=isolated_env, capture=True, echo_capture=False)
    try:
        tool_paths = json.loads(tool_paths_raw.strip().splitlines()[-1])
        purelib = Path(tool_paths['purelib']).resolve()
        tool_files = tool_paths.get('tool_files', [])
        if not isinstance(tool_files, list) or not all(isinstance(item, str) for item in tool_files):
            raise TypeError
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise GateError('PYTHON_TOOL_PATHS_UNRESOLVED: cannot locate isolated pytest') from exc
    if len(COMMAND_LOG) > probe_start:
        COMMAND_LOG[-1].update({
            'kind': 'wheel_test_toolchain',
            'probe_output_sha256': hashlib.sha256(tool_paths_raw.encode()).hexdigest(),
            'tool_file_count': len(tool_files),
            'output': json.dumps({'purelib': str(purelib),
                                  'tool_file_count': len(tool_files)}),
        })
    toolsite = scratch / 'test-tools'
    toolsite.mkdir()
    for relative in tool_files:
        source = (purelib / relative).resolve()
        try:
            source.relative_to(purelib)
        except ValueError as exc:
            raise GateError(f'PYTHON_TOOL_PATH_ESCAPE: {relative}') from exc
        if not source.is_file():
            raise GateError(f'PYTHON_TOOL_FILE_MISSING: {source}')
        destination = toolsite / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)

    smoke = r'''
import pathlib, sys
site = pathlib.Path(sys.argv[1]).resolve()
tests = pathlib.Path(sys.argv[2]).resolve()
source = pathlib.Path(sys.argv[3]).resolve()
toolsite = pathlib.Path(sys.argv[4]).resolve()
kept = []
for entry in sys.path:
    if not entry:
        continue
    resolved = pathlib.Path(entry).resolve()
    if resolved == source or source in resolved.parents:
        continue
    kept.append(entry)
sys.path[:] = [str(site), *kept, str(toolsite)]
if any((resolved := pathlib.Path(entry).resolve()) == source or source in resolved.parents
       for entry in sys.path if entry):
    raise RuntimeError('source project leaked into isolated sys.path')
import pytest
raise SystemExit(pytest.main(['-q', str(tests), '-c', '/dev/null']))
'''.strip()
    before = len(COMMAND_LOG)
    run(scratch, _python_invocation(python, project, '-I', '-S', '-c', smoke, str(site),
                                    str(smoke_tests), str(project), str(toolsite)),
        env=isolated_env, capture=True)
    _annotate_last_command(before, kind='wheel_public_api_smoke', tests=str(smoke_tests),
                           source_root=str(project), source_root_excluded=True,
                           target=str(site), import_names=imports)


def wheel_check(project: Path, python: list[str]) -> None:
    """Build, install and exercise only the wheel, never the source checkout."""
    if not (project / 'pyproject.toml').is_file():
        raise GateError('NO_PYPROJECT: wheel verification requires pyproject.toml')
    with tempfile.TemporaryDirectory(prefix='agentic-wheel-') as raw:
        scratch = Path(raw)
        dist = scratch / 'dist'
        site = scratch / 'site'
        dist.mkdir()
        site.mkdir()
        if python[:3] == ['uv', 'run', '--locked']:
            run(scratch, ['uv', 'build', '--wheel', '--no-build-isolation', '--offline',
                          '--no-python-downloads', '--out-dir', str(dist), str(project)], capture=True)
        else:
            run(scratch, [python[0], '-m', 'pip', 'wheel', '--no-deps', '--no-build-isolation',
                          '--wheel-dir', str(dist), str(project)], capture=True)
        wheels = list(dist.glob('*.whl'))
        if len(wheels) != 1:
            raise GateError(f'WHEEL_COUNT: expected one wheel, got {len(wheels)}')
        wheel_path = wheels[0]
        wheel = str(wheel_path)
        COMMAND_LOG.append({'kind': 'wheel_artifact', 'path': wheel_path.name,
                            'sha256': sha(wheel_path)})
        if python[:3] == ['uv', 'run', '--locked']:
            run(scratch, ['uv', 'pip', 'install', '--offline', '--target', str(site),
                          '--no-deps', wheel], capture=True)
        else:
            run(scratch, [python[0], '-m', 'pip', 'install', '--no-deps', '--target', str(site), wheel],
                capture=True)
        _isolated_wheel_checks(project, python, scratch, site, wheel_path)
    print('=== WHEEL EVIDENCE: PASS (artifact provenance and public API smoke isolated) ===')


def tdd_path(project: Path) -> Path:
    return task_state_dir(project) / 'tdd.json'


def validate_tdd_command(project: Path, command: list[str]) -> None:
    """Keep the evidence interface from becoming a general-purpose command proxy."""
    invoked = command[0]
    # A name-only check can be bypassed with ./pytest or /tmp/evil/python3:
    # the Runner would execute an arbitrary program through an allowed TDD call.
    if '/' in invoked or '\\' in invoked:
        trusted = {str(Path(sys.executable).absolute()),
                   str((project / '.venv/bin/python').absolute()),
                   str((project / '.venv/bin/pytest').absolute()),
                   str((project / '.venv/bin/py.test').absolute())}
        if not Path(invoked).is_absolute() or str(Path(invoked).absolute()) not in trusted:
            raise GateError('TDD_COMMAND_DENIED: executable must be a selected Python/pytest backend')
    executable = Path(invoked).name
    args = command[1:]
    direct = executable in {'pytest', 'py.test'}
    python_test = executable.startswith('python') and args[:2] in (['-m', 'pytest'], ['-m', 'unittest'])
    uv_test = executable == 'uv' and (
        args[:3] == ['run', '--locked', 'pytest']
        or args[:5] == ['run', '--locked', 'python', '-m', 'pytest']
        or args[:5] == ['run', '--locked', 'python', '-m', 'unittest']
    )
    if not (direct or python_test or uv_test):
        raise GateError('TDD_COMMAND_DENIED: use pytest/unittest directly, via Python -m, or uv run --locked')


def record_tdd(project: Path, cycle: str, stage: str, command: list[str],
               expected_failure: str | None = None, *, scope: str = 'feature') -> None:
    """Run and record a real TDD check; never manufacture retrospective evidence."""
    require_open_task(project)
    current_baseline = validate_baseline(project)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', cycle):
        raise GateError('INVALID_CYCLE: use 1-64 ASCII letters, digits, dot, underscore or dash')
    if scope not in {'feature', 'regression'}:
        raise GateError('INVALID_TDD_SCOPE: use feature or regression')
    if command[:1] == ['--']:
        command = command[1:]
    if not command:
        raise GateError('EMPTY_TDD_COMMAND')
    validate_tdd_command(project, command)
    expected_failure = expected_failure.strip() if expected_failure else None
    if stage == 'red' and (not expected_failure or len(expected_failure) > 500):
        raise GateError('RED_EXPECTATION_REQUIRED: describe the expected failure in 1-500 characters')
    path = tdd_path(project)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        data = json.loads(path.read_text()) if path.exists() else {
            'baseline': current_baseline, 'cycles': {},
        }
        if 'baseline' not in data:
            # Older Runner versions could not replace an approval, so binding their
            # immutable log to the then-current baseline is deterministic.
            if (task_state_dir(project) / 'approval-history').exists():
                raise GateError('TDD_BASELINE_CHANGED: legacy TDD predates the current approval; '
                                'retained evidence must not be rebound')
            data['baseline'] = current_baseline
        if data.get('baseline') != current_baseline:
            raise GateError('TDD_BASELINE_CHANGED: prior TDD is retained for audit; '
                            'do not manufacture retrospective evidence')
        entries = data['cycles'].setdefault(cycle, [])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise GateError('INVALID_TDD_LOG') from exc
    stage_order = ['red', 'green', 'refactor']
    target_index = stage_order.index(stage)
    if entries:
        try:
            current_stage = entries[-1]['stage']
            current_index = stage_order.index(current_stage)
            current_scopes = {entry.get('scope', 'feature') for entry in entries
                              if entry.get('stage') == current_stage}
        except (KeyError, ValueError, TypeError, AttributeError) as exc:
            raise GateError('INVALID_TDD_LOG') from exc
        if target_index == current_index:
            if scope in current_scopes:
                raise GateError(f'TDD_SCOPE_DUPLICATE: {stage}/{scope} is already recorded')
        elif target_index == current_index + 1 and 'feature' in current_scopes:
            pass
        else:
            raise GateError(f'TDD_ORDER: cannot record {stage}/{scope} after {current_stage}')
    elif stage != 'red':
        raise GateError(f'TDD_ORDER: {stage} requires a prior RED feature check')
    before = fingerprint(project)
    print(f'$ (cd {shlex.quote(str(project))} && {shlex.join(command)})', flush=True)
    try:
        done = subprocess.run(command, cwd=project, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, check=False)
    except FileNotFoundError as exc:
        raise GateError(f'MISSING_EXECUTABLE: {command[0]}') from exc
    print(done.stdout, end='', flush=True)
    expected_code = 1 if stage == 'red' else 0
    if done.returncode != expected_code:
        expectation = str(expected_code)
        raise GateFailure(f'TDD_{stage.upper()}_UNEXPECTED_EXIT: expected {expectation}, got {done.returncode}')
    entry = {'stage': stage, 'scope': scope, 'command': command, 'exit_code': done.returncode,
             'output_sha256': hashlib.sha256(done.stdout.encode()).hexdigest(),
             'output_tail': done.stdout[-4000:], 'fingerprint_before': before,
             'fingerprint_after': fingerprint(project),
             'recorded_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    if expected_failure:
        entry['expected_failure'] = expected_failure
    entries.append(entry)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(temp, path)
    print(f'TDD EVIDENCE: {cycle} {stage.upper()} {scope.upper()} recorded')


def collect_evidence(project: Path) -> dict[str, object]:
    """Load evidence and annotate freshness without trusting prose from a model."""
    task_id, _ = active_task(project)
    current_fingerprint = fingerprint(project)
    current_baseline = validate_baseline(project)
    receipts = {}
    try:
        for level in ('fast', 'verify', 'full'):
            path = receipt_path(project, level)
            if path.is_file():
                data = json.loads(path.read_text())
                receipt_baseline = data.get('baseline')
                if task_id == 'legacy' and isinstance(receipt_baseline, dict):
                    receipt_baseline = {'task_id': 'legacy', **receipt_baseline}
                receipt_task = data.get('task_id', 'legacy' if task_id == 'legacy' else None)
                data['current'] = (receipt_task == task_id
                                   and data.get('fingerprint') == current_fingerprint
                                   and receipt_baseline == current_baseline)
                receipts[level] = data
        tdd = json.loads(tdd_path(project).read_text()) if tdd_path(project).is_file() else {'cycles': {}}
    except (ValueError, TypeError, AttributeError) as exc:
        raise GateError('INVALID_EVIDENCE: damaged receipt or TDD log') from exc
    registry = load_registry(project)
    return {'state_store': str(task_state_dir(project)),
            'active_task': task_id, 'tasks': registry['tasks'],
            'current_fingerprint': current_fingerprint,
            'current_baseline': current_baseline,
            'sources': {
                'approved_spec': str(working_paths(project)[0]),
                'approved_plan': str(working_paths(project)[1]),
                'baseline_spec': str(approved_paths(project)[0]),
                'baseline_plan': str(approved_paths(project)[1]),
                'tdd': str(tdd_path(project)),
                'receipts': {level: str(receipt_path(project, level))
                             for level in ('fast', 'verify', 'full')},
            },
            'receipts': receipts, 'tdd': tdd}


def evidence(project: Path) -> None:
    """Print external evidence through the supported Runner interface."""
    print(json.dumps(collect_evidence(project), indent=2))


def _command_summary(entry: dict[str, object]) -> dict[str, object]:
    summary = {key: entry[key] for key in ('kind', 'cwd', 'command', 'exit_code', 'path',
                                           'sha256', 'output_sha256', 'output_tail', 'imports',
                                           'import_names', 'source_root',
                                           'source_root_excluded', 'target', 'tests',
                                           'probe_output_sha256', 'tool_file_count')
               if key in entry}
    output = entry.get('output')
    if isinstance(output, str):
        summary['output_sha256'] = hashlib.sha256(output.encode()).hexdigest()
        summary['output_tail'] = output[-1000:]
    elif isinstance(summary.get('output_tail'), str):
        summary['output_tail'] = summary['output_tail'][-1000:]
    return summary


def review_context(project: Path, level: str) -> None:
    """Emit compact, source-linked Runner evidence for an independent Reviewer."""
    data = collect_evidence(project)
    receipts = data['receipts']
    receipt = receipts.get(level) if isinstance(receipts, dict) else None
    if not isinstance(receipt, dict):
        raise GateError(f'NO_{level.upper()}_RECEIPT: сначала пройди соответствующий gate')
    if receipt.get('current') is not True:
        raise GateError(f'STALE_{level.upper()}_RECEIPT: код или approval изменился после проверки')
    commands = receipt.get('commands')
    if not isinstance(commands, list):
        raise GateError('INVALID_RECEIPT: commands missing')
    summaries = [_command_summary(entry) for entry in commands if isinstance(entry, dict)]
    artifact = [entry for entry in summaries if entry.get('kind') == 'wheel_artifact']
    build = [entry for entry in summaries if isinstance(entry.get('command'), list)
             and ('wheel' in entry['command'] or 'build' in entry['command'])]
    install = [entry for entry in summaries if isinstance(entry.get('command'), list)
               and 'install' in entry['command'] and '--target' in entry['command']]
    provenance = [entry for entry in summaries
                  if entry.get('kind') == 'wheel_import_provenance'
                  and entry.get('source_root_excluded') is True]
    isolated_test = [entry for entry in summaries
                     if entry.get('kind') == 'wheel_public_api_smoke'
                     and entry.get('source_root_excluded') is True
                     and Path(str(entry.get('cwd', project))).resolve() != project.resolve()]
    wheel = {
        'verified': bool(receipt.get('wheel_verified') and artifact and build and install
                         and provenance and isolated_test),
        'artifact': artifact,
        'build': build,
        'isolated_install': install,
        'import_provenance': provenance,
        'isolated_test': isolated_test,
        'note': 'Wheel is built in a temporary directory; no wheel in the worktree is expected.',
    }
    tdd = data['tdd']
    cycles = tdd.get('cycles', {}) if isinstance(tdd, dict) else {}
    cycle_summary = {}
    if isinstance(cycles, dict):
        for cycle, entries in cycles.items():
            records = entries if isinstance(entries, list) else []
            stages = list(dict.fromkeys(entry.get('stage') for entry in records
                                        if isinstance(entry, dict)))
            scopes = {stage: [entry.get('scope', 'feature') for entry in records
                              if isinstance(entry, dict) and entry.get('stage') == stage]
                      for stage in ('red', 'green', 'refactor')}
            cycle_summary[cycle] = {
                'stages': stages,
                'scopes': scopes,
                'complete': (stages == ['red', 'green', 'refactor']
                             and all('feature' in scopes[stage]
                                     for stage in ('red', 'green', 'refactor'))),
                'regression_complete': all('regression' in scopes[stage]
                                           for stage in ('red', 'green', 'refactor')),
                'records': [_command_summary(entry) | {
                    key: entry[key] for key in ('stage', 'scope', 'expected_failure', 'recorded_at',
                                                'fingerprint_before', 'fingerprint_after')
                    if key in entry
                } for entry in records if isinstance(entry, dict)],
            }
    sources = data['sources']
    assert isinstance(sources, dict) and isinstance(sources['receipts'], dict)
    history = task_state_dir(project) / 'approval-history'
    tdd_baseline = tdd.get('baseline') if isinstance(tdd, dict) else None
    tdd_baseline_current = (tdd_baseline == data['current_baseline']
                            or (tdd_baseline is None and not history.exists()))
    print(json.dumps({
        'active_task': data['active_task'],
        'task_audit': {
            'status_history': (data['tasks'].get(data['active_task'], {}).get('status_history', [])
                               if isinstance(data.get('tasks'), dict) else []),
            'reopen_archives': sorted(str(path) for path in
                                      (task_state_dir(project) / 'status-history').glob('*-reopen')),
            'note': 'Archived evidence is historical; current receipts and review bindings decide freshness.',
        },
        'approved_documents': sources,
        'approval': data['current_baseline'],
        'current_fingerprint': data['current_fingerprint'],
        'tdd': {
            'source': sources['tdd'],
            'baseline': tdd_baseline,
            'baseline_current': tdd_baseline_current,
            'binding': ('explicit' if tdd_baseline is not None else
                        'implicit-pre-reapproval' if not history.exists() else
                        'superseded-approval-only'),
            'cycles': cycle_summary,
        },
        'receipt': {
            'source': sources['receipts'][level],
            'level': level,
            'current': True,
            'fingerprint': receipt.get('fingerprint'),
            'baseline': receipt.get('baseline'),
            'passed_at': receipt.get('passed_at'),
            'commands': summaries,
        },
        'wheel': wheel,
    }, indent=2))


def tool_path(name: str) -> Path:
    tools = Path(os.environ.get('AGENTIC_TOOLS_DIR', str(Path.home() / '.local/share/opencode-agentic/tools')))
    exe = tools / name / name
    if not exe.is_file():
        raise GateError(f'MISSING_QUALITY_TOOL: {exe}. Запусти bootstrap-tools.sh')
    return exe


def source_targets(project: Path) -> tuple[list[str], list[str]]:
    """Conservative Python targets, including flat layout without collecting tests as source."""
    tests = project / 'tests'
    if not tests.is_dir() or not list(tests.rglob('test_*.py')):
        raise GateError('NO_TESTS: tests/test_*.py не найдены')
    src = project / 'src'
    if src.is_dir() and any(src.rglob('*.py')):
        return ['src'], ['src', 'tests']
    modules = sorted(f.name for f in project.glob('*.py')
                     if f.name not in {'setup.py', 'conftest.py'} and not f.name.startswith('test_'))
    packages = sorted(d.name for d in project.iterdir()
                      if d.is_dir() and d.name not in {'tests', 'test'} and (d / '__init__.py').is_file())
    targets = modules + packages
    if not targets:
        raise GateError('NO_PYTHON_SOURCES: нет src или корневых модулей/packages')
    return targets, [*targets, 'tests']


def python_backend(project: Path) -> tuple[list[str], list[str]]:
    """Choose locked uv or existing Python environment; never install or create lockfiles."""
    if (project / 'uv.lock').is_file():
        if not shutil.which('uv'):
            raise GateError('MISSING_QUALITY_TOOL: uv недоступен')
        return ['uv', 'run', '--locked'], ['uv', 'run', '--locked']
    candidate = project / '.venv/bin/python'
    interpreter = str(candidate) if candidate.is_file() else sys.executable
    return [interpreter, '-m'], [interpreter]


def require_module(project: Path, runner: list[str], module: str) -> None:
    if runner[:3] == ['uv', 'run', '--locked']:
        return  # uv reports the missing tool accurately at execution time.
    proc = subprocess.run([runner[0], '-c',
                           f'import importlib.util; assert importlib.util.find_spec({module!r}) is not None'],
                          cwd=project, capture_output=True)
    if proc.returncode:
        raise GateError(f'MISSING_QUALITY_TOOL: Python module {module}')


def pytest_command(backend: list[str], *args: str) -> list[str]:
    """Return the one project-selected pytest command used by gates and tools."""
    return [*backend, 'pytest', *args]


def baseline_test(project: Path) -> None:
    """Run the approved project's normal baseline without exposing arbitrary Python."""
    require_open_task(project)
    validate_baseline(project)
    backend, python = python_backend(project)
    if backend != ['uv', 'run', '--locked']:
        require_module(project, python, 'pytest')
    run(project, pytest_command(backend, '-q', 'tests'))


def mutator_command(project: Path, backend: list[str], sources: list[str]) -> list[str]:
    """Bind Mutator to the same pytest backend as the quality gate."""
    test = shlex.join(pytest_command(backend, '-q', 'tests'))
    return [str(tool_path('mutator')), '--use-existing-coverage', '--test-command', test,
            '--max-workers', '2', *sources]


def quality(project: Path, level: str) -> None:
    print(f'=== QUALITY GATE: {level.upper()} ===')
    validate_baseline(project)
    sources, lint = source_targets(project)
    backend, python = python_backend(project)
    if backend == ['uv', 'run', '--locked']:
        def module_cmd(name: str, *args: str) -> list[str]:
            return backend + [name, *args]
    else:
        for module in ['ruff', 'mypy', 'pytest'] + (['pytest_cov'] if level != 'fast' else []):
            require_module(project, python, module)
        def module_cmd(name: str, *args: str) -> list[str]:
            return backend + [name, *args]

    python_version = ([*python, 'python', '--version']
                      if python[:3] == ['uv', 'run', '--locked'] else [python[0], '--version'])
    run(project, python_version, capture=True)
    for tool in ('ruff', 'mypy', 'pytest'):
        run(project, module_cmd(tool, '--version'), capture=True)

    run(project, module_cmd('ruff', 'check', *lint))
    run(project, module_cmd('ruff', 'format', '--check', *lint))
    run(project, module_cmd('mypy', *sources))
    if level == 'fast':
        run(project, module_cmd('pytest', '-q', 'tests'))
    else:
        report = project / 'target/coverage/python/lcov.info'
        report.parent.mkdir(parents=True, exist_ok=True)
        coverage_args = ['--cov=src'] if sources == ['src'] else [
            '--cov=' + (p[:-3] if p.endswith('.py') else p) for p in sources]
        run(project, module_cmd('pytest', '-q', 'tests', *coverage_args,
                                f'--cov-report=lcov:{report}'))
        if not report.is_file() or report.stat().st_size == 0:
            raise GateError('MISSING_COVERAGE_ARTIFACT: LCOV отсутствует или пуст')
        run(project, [str(tool_path('crapper')), '--use-existing-coverage', '--threshold', '10', *sources])
        duplicates = run(project, [str(tool_path('dryer')), '--threshold', '0.8', *sources], capture=True)
        if re.search(r'^DUPLICATE\s+score=', duplicates, re.MULTILINE):
            raise GateFailure('DRYER_DUPLICATES: дублирование выше 0.8; требуется разбор')
        if level == 'full':
            run(project, mutator_command(project, backend, sources))
        wheel_check(project, python)
    validate_baseline(project)
    print(f'=== QUALITY GATE {level.upper()}: PASS ===')


def receipt_path(project: Path, level: str) -> Path:
    return task_state_dir(project) / f'{level}.json'


def gate(project: Path, level: str) -> None:
    task_id = require_open_task(project)
    # A preflight approval error must not destroy the last receipt before a
    # replacement approval has archived it. Once quality execution begins, any
    # failure still invalidates the previous PASS below.
    validate_baseline(project)
    COMMAND_LOG.clear()
    receipt = receipt_path(project, level)
    receipt.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if receipt.exists():
        receipt.unlink()  # stale PASS cannot survive a failed rerun
    before = fingerprint(project)
    quality(project, level)
    after = fingerprint(project)
    if before != after:
        raise GateError('WORKTREE_CHANGED_DURING_GATE: повтори проверку после стабилизации кода')
    # On success only: avoid stale PASS when agent edits code next.
    baseline = validate_baseline(project)
    data = {'task_id': task_id, 'level': level, 'fingerprint': after, 'baseline': baseline,
            'commands': list(COMMAND_LOG), 'wheel_verified': level != 'fast',
            'passed_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    temp = receipt.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(temp, receipt)


def current_receipt(project: Path, level: str) -> dict[str, object]:
    """Return a current receipt after validating its task, approval and worktree binding."""
    task_id, task = active_task(project)
    path = receipt_path(project, level)
    if not path.is_file():
        raise GateError(f'NO_{level.upper()}_RECEIPT: сначала пройди соответствующий gate')
    try:
        data = json.loads(path.read_text())
        receipt_task = data.get('task_id')
        if receipt_task is None and task.get('layout') == 'legacy':
            receipt_task = 'legacy'
        receipt_fingerprint = data['fingerprint']
        receipt_baseline = data['baseline']
    except (ValueError, KeyError, TypeError) as exc:
        raise GateError('INVALID_RECEIPT: damaged or incompatible receipt') from exc
    if receipt_task != task_id:
        raise GateError(f'WRONG_TASK_RECEIPT: expected {task_id}, got {receipt_task}')
    if task.get('layout') == 'legacy' and isinstance(receipt_baseline, dict):
        receipt_baseline = {'task_id': 'legacy', **receipt_baseline}
    if receipt_fingerprint != fingerprint(project) or receipt_baseline != validate_baseline(project):
        raise GateError(f'STALE_{level.upper()}_RECEIPT: код изменился после проверки')
    return data


def check_receipt(project: Path, level: str) -> None:
    current_receipt(project, level)
    print(f'PASS: {level} проверен для текущего состояния Git/worktree')


REVIEW_ID = re.compile(r'[0-9a-f]{32}')


def review_path(project: Path, review_id: str) -> Path:
    if not REVIEW_ID.fullmatch(review_id):
        raise GateError('INVALID_REVIEW_ID: expected the id printed by /finalize')
    return task_state_dir(project) / 'reviews' / f'{review_id}.json'


def current_review_path(project: Path) -> Path:
    return task_state_dir(project) / 'review-current.json'


def current_verify_review_path(project: Path) -> Path:
    return task_state_dir(project) / 'review-verify-current.json'


def _read_json_object(path: Path, error: str) -> dict[str, object]:
    try:
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise TypeError
        return value
    except (OSError, ValueError, TypeError) as exc:
        raise GateError(error) from exc


def _review_binding(project: Path, level: str = 'full') -> dict[str, object]:
    task_id, _ = active_task(project)
    receipt = current_receipt(project, level)
    return {
        'task_id': task_id,
        'approval': validate_baseline(project),
        'fingerprint': fingerprint(project),
        f'{level}_receipt': str(receipt_path(project, level)),
        f'{level}_receipt_sha256': sha(receipt_path(project, level)),
        f'{level}_passed_at': receipt.get('passed_at'),
    }


def _same_review_subject(left: object, right: object) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return all(left.get(key) == right.get(key)
               for key in ('task_id', 'approval', 'fingerprint'))


def _pointer_record(project: Path, pointer_path: Path) -> dict[str, object] | None:
    if not pointer_path.is_file():
        return None
    pointer = _read_json_object(pointer_path, 'INVALID_CURRENT_REVIEW: damaged pointer')
    review_id = pointer.get('review_id')
    if not isinstance(review_id, str) or not REVIEW_ID.fullmatch(review_id):
        return None
    path = review_path(project, review_id)
    if not path.is_file():
        raise GateError('INVALID_CURRENT_REVIEW: review record is missing')
    return _read_json_object(path, 'INVALID_REVIEW: damaged review record')


def _supersede_pending(project: Path, pointer_path: Path, replacement: str) -> None:
    prior = _pointer_record(project, pointer_path)
    if prior is None or prior.get('status') != 'pending':
        return
    prior['status'] = 'superseded'
    prior['checks_completed'] = False
    prior['superseded_by'] = replacement
    prior['submitted_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
    _write_json_atomic(review_path(project, str(prior['review_id'])), prior)


def _current_independent_review(project: Path,
                                full_binding: dict[str, object]) -> dict[str, object] | None:
    record = _pointer_record(project, current_verify_review_path(project))
    if record is None or record.get('phase') != 'verify':
        return None
    if not _same_review_subject(record.get('binding'), full_binding):
        if record.get('status') not in {'stale', 'superseded'}:
            record['previous_status'] = record.get('status')
            record['status'] = 'stale'
            record['checks_completed'] = False
            record['invalidated_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            record['invalidation_reason'] = 'task, approval or fingerprint changed after /review'
            _write_json_atomic(review_path(project, str(record['review_id'])), record)
        return None
    if record.get('status') == 'pending':
        raise GateError('VERIFY_REVIEW_PENDING: дождись заключения /review перед /finalize')
    if record.get('status') not in {'passed', 'rejected'} or record.get('checks_completed') is not True:
        return None
    return record


def begin_review(project: Path, phase: str = 'finalize') -> dict[str, object]:
    """Create a VERIFY review or a FINALIZE check bound to current evidence."""
    if phase not in {'verify', 'finalize'}:
        raise GateError(f'INVALID_REVIEW_PHASE: {phase}')
    require_open_task(project)
    level = 'verify' if phase == 'verify' else 'full'
    binding = _review_binding(project, level)
    current = (current_verify_review_path(project) if phase == 'verify'
               else current_review_path(project))
    if current.is_file():
        existing = _pointer_record(project, current)
        if (existing is not None and existing.get('phase') == phase
                and existing.get('binding') == binding
                and existing.get('status') in {'pending', 'passed'}):
            return {'created': False, **existing}
    review_id = secrets.token_hex(16)
    source = _current_independent_review(project, binding) if phase == 'finalize' else None
    mode = ('independent-review' if phase == 'verify' else
            'reuse-independent-review' if source is not None else
            'full-independent-review')
    record: dict[str, object] = {
        'version': 2,
        'review_id': review_id,
        'phase': phase,
        'mode': mode,
        'status': 'pending',
        'binding': binding,
        'source_review_id': source.get('review_id') if source is not None else None,
        'carried_findings': source.get('findings', []) if source is not None else [],
        'started_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    }
    path = review_path(project, review_id)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    _supersede_pending(project, current, review_id)
    _write_json_atomic(path, record)
    _write_json_atomic(current, {'review_id': review_id})
    return {'created': True, **record}


def review_status(project: Path) -> dict[str, object]:
    """Report whether a current pending/passed review already owns this FULL receipt."""
    require_open_task(project)
    pointer_path = current_review_path(project)
    if not pointer_path.is_file():
        return {'current': False, 'status': 'none'}
    pointer = _read_json_object(pointer_path, 'INVALID_CURRENT_REVIEW: damaged pointer')
    review_id = pointer.get('review_id')
    if not isinstance(review_id, str) or not REVIEW_ID.fullmatch(review_id):
        return {'current': False, 'status': 'none'}
    path = review_path(project, review_id)
    if not path.is_file():
        raise GateError('INVALID_CURRENT_REVIEW: review record is missing')
    record = _read_json_object(path, 'INVALID_REVIEW: damaged review record')
    try:
        current = (record.get('phase') == 'finalize'
                   and record.get('binding') == _review_binding(project, 'full'))
    except GateError:
        current = False
    return {'current': current, **record}


FINDING = re.compile(r'(major|minor|process):([^\s]{1,240})')


def _parse_findings(tokens: list[str]) -> list[dict[str, str]]:
    if tokens == ['none']:
        return []
    findings = []
    for token in tokens:
        match = FINDING.fullmatch(token)
        if not match:
            raise GateError('INVALID_FINDING: use severity:concrete-location-or-reason without spaces')
        findings.append({'severity': match.group(1), 'detail': match.group(2)})
    return findings


def submit_review(project: Path, review_id: str, verdict: str,
                  finding_tokens: list[str] | None = None) -> dict[str, object]:
    """Record the independent Reviewer outcome; exceptions are never represented as PASS."""
    path = review_path(project, review_id)
    if not path.is_file():
        raise GateError(f'UNKNOWN_REVIEW: {review_id}')
    record = _read_json_object(path, 'INVALID_REVIEW: damaged review record')
    findings = _parse_findings(finding_tokens or [])
    if verdict in {'reject', 'exception'} and not findings:
        raise GateError('FINDINGS_REQUIRED: reject/exception requires a concrete finding')
    if verdict == 'pass' and any(item['severity'] in {'major', 'process'} for item in findings):
        raise GateError('PASS_WITH_BLOCKING_FINDINGS: major/process findings cannot be PASS')
    carried = record.get('carried_findings', [])
    if verdict == 'pass' and isinstance(carried, list):
        blockers = {item.get('severity') for item in carried if isinstance(item, dict)}
        if 'major' in blockers:
            raise GateError('UNRESOLVED_MAJOR_FINDINGS: change the code and obtain a current review')
        if 'process' in blockers:
            raise GateError('UNRESOLVED_PROCESS_FINDINGS: resolve the process finding and obtain a current review')
    status = {'pass': 'passed', 'reject': 'rejected', 'exception': 'exception'}[verdict]
    existing = record.get('status')
    if existing != 'pending':
        if existing == status:
            return {'already_submitted': True, **record}
        raise GateError(f'REVIEW_ALREADY_SUBMITTED: {review_id} is {existing}')
    phase = record.get('phase')
    pointer_path = (current_verify_review_path(project) if phase == 'verify'
                    else current_review_path(project))
    pointer = _read_json_object(pointer_path,
                                'INVALID_CURRENT_REVIEW: damaged pointer')
    if pointer.get('review_id') != review_id:
        raise GateError('SUPERSEDED_REVIEW: only the current /finalize review may be submitted')
    try:
        binding = _review_binding(project, 'verify' if phase == 'verify' else 'full')
    except GateError as exc:
        record.update({
            'status': 'stale',
            'submitted_verdict': verdict,
            'checks_completed': False,
            'process_exception': str(exc),
            'submitted_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        })
        _write_json_atomic(path, record)
        raise GateError(f'STALE_REVIEW: {exc}') from exc
    if record.get('binding') != binding:
        record.update({
            'status': 'stale',
            'submitted_verdict': verdict,
            'checks_completed': False,
            'process_exception': 'review binding no longer matches current FULL receipt',
            'submitted_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        })
        _write_json_atomic(path, record)
        raise GateError('STALE_REVIEW: task, approval, fingerprint or FULL receipt changed')
    record.update({
        'status': status,
        'submitted_verdict': verdict,
        'checks_completed': verdict in {'pass', 'reject'},
        'findings': findings,
        'submitted_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
    })
    if verdict == 'exception':
        record['process_exception'] = 'Reviewer reported an incomplete review process'
    _write_json_atomic(path, record)
    return {'already_submitted': False, **record}


def accept_review(project: Path, review_id: str) -> dict[str, object]:
    """Atomically complete the active task only after a current passed review."""
    task_id, task = active_task(project)
    if task.get('completed_at') is not None:
        acceptance = task.get('acceptance')
        if isinstance(acceptance, dict) and acceptance.get('review_id') == review_id:
            return {'already_completed': True, 'task_id': task_id, **acceptance}
        if not isinstance(acceptance, dict):
            raise GateError('LEGACY_PREMATURE_COMPLETION: task has completed_at but no acceptance audit; '
                            'use /task reopen <id>, never edit tasks.json manually')
        raise GateError(f'TASK_COMPLETED_BY_DIFFERENT_REVIEW: {task_id}')
    path = review_path(project, review_id)
    if not path.is_file():
        raise GateError(f'UNKNOWN_REVIEW: {review_id}')
    record = _read_json_object(path, 'INVALID_REVIEW: damaged review record')
    if record.get('phase') != 'finalize':
        raise GateError('REVIEW_NOT_FINAL: /accept requires the Review ID printed by /finalize')
    if record.get('status') != 'passed' or record.get('checks_completed') is not True:
        raise GateError(f'REVIEW_NOT_PASSED: {review_id} is {record.get("status")}')
    pointer = _read_json_object(current_review_path(project),
                                'INVALID_CURRENT_REVIEW: damaged pointer')
    if pointer.get('review_id') != review_id:
        raise GateError('SUPERSEDED_REVIEW: human acceptance requires the current review')
    binding = _review_binding(project, 'full')
    if record.get('binding') != binding:
        raise GateError('STALE_REVIEW: task, approval, fingerprint or FULL receipt changed')
    accepted_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    data = load_registry(project)
    tasks = data['tasks']
    assert isinstance(tasks, dict) and isinstance(tasks[task_id], dict)
    acceptance = {'review_id': review_id, 'accepted_at': accepted_at, **binding}
    tasks[task_id]['completed_at'] = accepted_at
    tasks[task_id]['acceptance'] = acceptance
    _write_json_atomic(registry_path(project), data)
    return {'already_completed': False, 'task_id': task_id, **acceptance}


def reopen_task(project: Path, task_id: str) -> dict[str, object]:
    """Recover a prematurely completed task while preserving an explicit status audit."""
    data = load_registry(project)
    tasks = data['tasks']
    if not isinstance(tasks, dict) or task_id not in tasks or not isinstance(tasks[task_id], dict):
        raise GateError(f'UNKNOWN_TASK: {task_id}')
    task = tasks[task_id]
    previous = task.get('completed_at')
    if previous is None:
        raise GateError(f'TASK_NOT_COMPLETED: {task_id}')
    reason = ('human-reopened-after-acceptance' if isinstance(task.get('acceptance'), dict)
              else 'premature-finalize-without-human-acceptance')
    reopened_at = datetime.now(timezone.utc).isoformat(timespec='seconds')
    target_state = (state_dir(project) if task.get('layout') == 'legacy'
                    else state_dir(project) / 'tasks' / task_id)
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    archive = target_state / 'status-history' / f'{stamp}-reopen'
    archive.mkdir(parents=True, mode=0o700)
    copied = []
    for name in ('tdd.json', 'fast.json', 'verify.json', 'full.json', 'review-current.json',
                 'review-verify-current.json'):
        source = target_state / name
        if source.is_file():
            shutil.copy2(source, archive / name)
            copied.append(name)
    reviews = target_state / 'reviews'
    if reviews.is_dir():
        shutil.copytree(reviews, archive / 'reviews')
        copied.append('reviews/')
    _write_json_atomic(archive / 'reopen.json', {
        'reopened_at': reopened_at,
        'reason': reason,
        'task_before': json.loads(json.dumps(task)),
        'evidence_files': copied,
    })
    history = task.setdefault('status_history', [])
    if not isinstance(history, list):
        raise GateError('INVALID_TASK_REGISTRY: status_history must be a list')
    history.append({
        'event': 'reopened',
        'at': reopened_at,
        'reason': reason,
        'previous_completed_at': previous,
        'previous_acceptance': task.get('acceptance'),
        'archive': str(archive),
    })
    task['completed_at'] = None
    task.pop('acceptance', None)
    data['active_task'] = task_id
    _write_json_atomic(registry_path(project), data)
    _write_json_atomic(current_review_path(project), {
        'review_id': None,
        'invalidated_at': reopened_at,
        'reason': 'task reopened; a new /finalize review is required',
    })
    _write_json_atomic(current_verify_review_path(project), {
        'review_id': None,
        'invalidated_at': reopened_at,
        'reason': 'task reopened; historical /review cannot be reused',
    })
    return {'task_id': task_id, 'reopened_at': reopened_at,
            'previous_completed_at': previous, 'archive': str(archive)}


def complete_task(project: Path) -> None:
    raise GateError('DIRECT_COMPLETE_DISABLED: use /finalize, wait for Reviewer, then /accept <review-id>')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='OpenCode/Codex deterministic gates')
    parser.add_argument('--project', type=Path, default=Path.cwd())
    command = parser.add_subparsers(dest='command', required=True)
    approve_parser = command.add_parser('approve')
    approve_parser.add_argument('--human-confirm', action='store_true')
    approve_parser.add_argument('--replace-existing', action='store_true')
    command.add_parser('check-approval')
    command.add_parser('task-ensure')
    command.add_parser('task-info')
    command.add_parser('implement-context')
    new_task = command.add_parser('task-new')
    new_task.add_argument('task_id')
    use_task = command.add_parser('task-use')
    use_task.add_argument('task_id')
    reopen = command.add_parser('task-reopen')
    reopen.add_argument('task_id')
    reopen.add_argument('--human-confirm', action='store_true')
    command.add_parser('task-status')
    command.add_parser('complete')
    command.add_parser('fingerprint')
    command.add_parser('baseline')
    sub = command.add_parser('gate')
    sub.add_argument('level', choices=['fast', 'verify', 'full'])
    ready = command.add_parser('ready')
    ready.add_argument('level', choices=['verify', 'full'])
    tdd = command.add_parser('tdd')
    tdd.add_argument('--cycle', required=True)
    tdd.add_argument('--stage', required=True, choices=['red', 'green', 'refactor'])
    tdd.add_argument('--scope', choices=['feature', 'regression'], default='feature')
    tdd.add_argument('--expect')
    tdd.add_argument('test_command', nargs=argparse.REMAINDER)
    command.add_parser('evidence')
    review = command.add_parser('review-context')
    review.add_argument('level', choices=['verify', 'full'])
    start_review = command.add_parser('review-start')
    start_review.add_argument('phase', choices=['verify', 'finalize'])
    command.add_parser('review-status')
    submit = command.add_parser('review-submit')
    submit.add_argument('review_id')
    submit.add_argument('verdict', choices=['pass', 'reject', 'exception'])
    submit.add_argument('findings', nargs='*')
    accept = command.add_parser('accept')
    accept.add_argument('review_id')
    accept.add_argument('--human-confirm', action='store_true')
    args = parser.parse_args(argv)
    try:
        project = args.project.resolve(strict=True)
        validate_git_root(project)
        if args.command == 'approve':
            if not args.human_confirm:
                raise GateError('HUMAN_REQUIRED: --human-confirm обязателен')
            approve(project, replace_existing=args.replace_existing)
        elif args.command == 'check-approval':
            require_open_task(project)
            print(json.dumps(validate_baseline(project), indent=2))
        elif args.command == 'task-ensure':
            task_info(project, create=True)
        elif args.command == 'task-info':
            task_info(project)
        elif args.command == 'implement-context':
            implement_context(project)
        elif args.command == 'task-new':
            task_new(project, args.task_id)
        elif args.command == 'task-use':
            task_use(project, args.task_id)
        elif args.command == 'task-reopen':
            if not args.human_confirm:
                raise GateError('HUMAN_REQUIRED: --human-confirm обязателен')
            print(json.dumps(reopen_task(project, args.task_id), indent=2))
        elif args.command == 'task-status':
            task_status(project)
        elif args.command == 'complete':
            complete_task(project)
        elif args.command == 'fingerprint':
            print(fingerprint(project))
        elif args.command == 'baseline':
            baseline_test(project)
        elif args.command == 'gate':
            gate(project, args.level)
        elif args.command == 'ready':
            check_receipt(project, args.level)
        elif args.command == 'tdd':
            record_tdd(project, args.cycle, args.stage, args.test_command, args.expect,
                       scope=args.scope)
        elif args.command == 'evidence':
            evidence(project)
        elif args.command == 'review-context':
            review_context(project, args.level)
        elif args.command == 'review-start':
            print(json.dumps(begin_review(project, args.phase), indent=2))
        elif args.command == 'review-status':
            print(json.dumps(review_status(project), indent=2))
        elif args.command == 'review-submit':
            print(json.dumps(submit_review(project, args.review_id, args.verdict,
                                           args.findings), indent=2))
        elif args.command == 'accept':
            if not args.human_confirm:
                raise GateError('HUMAN_REQUIRED: --human-confirm обязателен')
            print(json.dumps(accept_review(project, args.review_id), indent=2))
    except GateFailure as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        return 1
    except (GateError, OSError, subprocess.CalledProcessError) as exc:
        print('BLOCKED: ' + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
