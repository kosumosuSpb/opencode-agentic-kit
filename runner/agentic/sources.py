"""Bounded discovery and classification of project-owned Python sources."""

from __future__ import annotations

import os
from pathlib import Path

from .errors import GateError
from .project import ROOT_GENERATED_DIRS

PYTHON_EXCLUDED_DIRS = ROOT_GENERATED_DIRS | frozenset({'venv', '__pycache__', '.tox', 'dist'})


def _bounded_directories(current: Path, directories: list[str]) -> list[str]:
    return sorted(
        name
        for name in directories
        if name not in PYTHON_EXCLUDED_DIRS and not (current / name).is_symlink()
    )


def _python_files(project: Path, root: Path, current: Path, files: list[str]) -> list[str]:
    discovered: list[str] = []
    for name in sorted(files):
        candidate = current / name
        if candidate.suffix != '.py' or candidate.is_symlink() or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        if resolved == root or root in resolved.parents:
            discovered.append(candidate.relative_to(project).as_posix())
    return discovered


def discover_python_files(project: Path) -> list[str]:
    """Return project-owned Python files without following links or generated trees."""
    root = project.resolve()
    discovered: list[str] = []
    for current, directories, files in os.walk(project, topdown=True, followlinks=False):
        current_path = Path(current)
        directories[:] = _bounded_directories(current_path, directories)
        discovered.extend(_python_files(project, root, current_path, files))
    return sorted(set(discovered))


def _is_python_test(path: str) -> bool:
    relative = Path(path)
    return (
        any(part in {'tests', 'test'} for part in relative.parts[:-1])
        or relative.name == 'conftest.py'
        or relative.name.startswith('test_')
    )


def source_targets(project: Path) -> tuple[list[str], list[str]]:
    """Return exact production and lint files from one bounded Python discovery pass."""
    discovered = discover_python_files(project)
    tests = [
        path
        for path in discovered
        if Path(path).parts[0] == 'tests' and Path(path).name.startswith('test_')
    ]
    if not tests:
        raise GateError('NO_TESTS: tests/test_*.py не найдены')
    targets = [path for path in discovered if path != 'setup.py' and not _is_python_test(path)]
    if not targets:
        raise GateError('NO_PYTHON_SOURCES: нет src или корневых модулей/packages')
    return targets, discovered
