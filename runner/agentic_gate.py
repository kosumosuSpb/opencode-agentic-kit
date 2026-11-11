#!/usr/bin/env python3
"""Deterministic checks and workflow state for OpenCode/Codex. Python 3.11+."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone

EXCLUDE_DIRS = {'.git', '.venv', '.agentic', '.metrics', 'target', '.mypy_cache', '.pytest_cache', '.ruff_cache', '__pycache__', 'node_modules'}


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


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def approved_paths(project: Path) -> tuple[Path, Path]:
    local = state_dir(project) / 'baseline'
    return local / 'spec.md', local / 'plan.md'


def working_paths(project: Path) -> tuple[Path, Path]:
    return project / 'specs/approved.md', project / 'plans/approved.md'


def validate_baseline(project: Path) -> dict[str, str]:
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
    return {'spec_sha256': expected[0], 'plan_sha256': expected[1]}


def approve(project: Path) -> None:
    baseline = approved_paths(project)
    working = working_paths(project)
    if all(path.is_file() for path in baseline):
        print('APPROVED: уже существует baseline, проверяем неизменность')
        print(json.dumps(validate_baseline(project), indent=2))
        return
    if any(path.exists() or path.is_symlink() for path in baseline):
        raise GateError('PARTIAL_BASELINE: повреждённый/частичный baseline; отказ')
    if any(f.exists() or f.is_symlink() for f in working):
        raise GateError('EXISTING_APPROVED_PATH: файлы specs/approved.md и plans/approved.md уже существуют; не перезаписываю')
    drafts = [project / '.agentic/drafts/spec.md', project / '.agentic/drafts/plan.md']
    if any(f.is_symlink() for f in drafts):
        raise GateError('SYMLINK_DRAFT: черновик не может быть символьной ссылкой')
    if not all(f.is_file() and f.stat().st_size for f in drafts):
        raise GateError('DRAFT_MISSING: нужны .agentic/drafts/spec.md и plan.md')
    # Reject directory symlinks; otherwise writes could escape the project.
    parents = [project / '.agentic', project / '.agentic/drafts',
               working[0].parent, working[1].parent,
               baseline[0].parent]
    if any(p.is_symlink() for p in parents):
        raise GateError('SYMLINK_DIRECTORY: approval path must not be a symlink')
    # Collect inputs and create every parent before the first output file.
    contents = [source.read_bytes() for source in drafts]
    for parent in {f.parent for f in [*working, *baseline]}:
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
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
        if any(part in EXCLUDE_DIRS for part in parts) or relative in {'.coverage'}:
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


def run(project: Path, command: list[str], *, capture: bool = False) -> str:
    print('$ ' + ' '.join(command), flush=True)
    try:
        done = subprocess.run(command, cwd=project, text=True, stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.STDOUT if capture else None, check=False)
    except FileNotFoundError as e:
        raise GateError(f'MISSING_EXECUTABLE: {command[0]}') from e
    if capture:
        print(done.stdout, end='', flush=True)
    if done.returncode:
        raise GateFailure(f'COMMAND_FAILED(exit={done.returncode}): {" ".join(command)}')
    return done.stdout if capture else ''


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
            run(project, [str(tool_path('mutator')), '--use-existing-coverage', '--max-workers', '2', *sources])
    validate_baseline(project)
    print(f'=== QUALITY GATE {level.upper()}: PASS ===')


def receipt_path(project: Path, level: str) -> Path:
    return state_dir(project) / f'{level}.json'


def gate(project: Path, level: str) -> None:
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
    data = {'level': level, 'fingerprint': after, 'baseline': baseline,
            'passed_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
    temp = receipt.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2) + '\n')
    os.replace(temp, receipt)


def check_receipt(project: Path, level: str) -> None:
    path = receipt_path(project, level)
    if not path.is_file():
        raise GateError(f'NO_{level.upper()}_RECEIPT: сначала пройди соответствующий gate')
    try:
        data = json.loads(path.read_text())
        receipt_fingerprint = data['fingerprint']
        receipt_baseline = data['baseline']
    except (ValueError, KeyError, TypeError) as exc:
        raise GateError('INVALID_RECEIPT: damaged or incompatible receipt') from exc
    if receipt_fingerprint != fingerprint(project) or receipt_baseline != validate_baseline(project):
        raise GateError(f'STALE_{level.upper()}_RECEIPT: код изменился после проверки')
    print(f'PASS: {level} проверен для текущего состояния Git/worktree')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='OpenCode/Codex deterministic gates')
    parser.add_argument('--project', type=Path, default=Path.cwd())
    command = parser.add_subparsers(dest='command', required=True)
    command.add_parser('approve').add_argument('--human-confirm', action='store_true')
    command.add_parser('check-approval')
    command.add_parser('fingerprint')
    sub = command.add_parser('gate')
    sub.add_argument('level', choices=['fast', 'verify', 'full'])
    ready = command.add_parser('ready')
    ready.add_argument('level', choices=['verify', 'full'])
    args = parser.parse_args(argv)
    try:
        project = args.project.resolve(strict=True)
        validate_git_root(project)
        if args.command == 'approve':
            if not args.human_confirm:
                raise GateError('HUMAN_REQUIRED: --human-confirm обязателен')
            approve(project)
        elif args.command == 'check-approval':
            print(json.dumps(validate_baseline(project), indent=2))
        elif args.command == 'fingerprint':
            print(fingerprint(project))
        elif args.command == 'gate':
            gate(project, args.level)
        elif args.command == 'ready':
            check_receipt(project, args.level)
    except GateFailure as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        return 1
    except (GateError, OSError, subprocess.CalledProcessError) as exc:
        print('BLOCKED: ' + str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
