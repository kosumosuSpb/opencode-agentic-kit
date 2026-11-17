"""Project identity, validation, state paths, and worktree fingerprinting."""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Protocol

from .errors import GateError

ROOT_GENERATED_DIRS = frozenset(
    {
        '.git',
        '.venv',
        '.agentic',
        '.metrics',
        'target',
        'build',
        '.mypy_cache',
        '.pytest_cache',
        '.ruff_cache',
        'node_modules',
    }
)


class DigestWriter(Protocol):
    def update(self, value: bytes) -> None: ...


def identity(project: Path) -> str:
    return hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:20]


def validate_git_root(project: Path) -> None:
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--show-toplevel'],
            cwd=project,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise GateError('NOT_GIT_REPOSITORY: требуется git init') from exc
    if Path(result.stdout.strip()).resolve() != project.resolve():
        raise GateError('PROJECT_NOT_GIT_ROOT: укажи git top-level, а не подкаталог')


def state_dir(project: Path) -> Path:
    root = Path(
        os.environ.get('AGENTIC_STATE_DIR', str(Path.home() / '.local/state/opencode-agentic'))
    )
    return root / 'projects' / identity(project)


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _is_generated(relative: str) -> bool:
    parts = Path(relative).parts
    generated_egg_info = parts[0].endswith('.egg-info') or (
        len(parts) > 1 and parts[0] == 'src' and parts[1].endswith('.egg-info')
    )
    return (
        parts[0] in ROOT_GENERATED_DIRS
        or '__pycache__' in parts
        or relative == '.coverage'
        or generated_egg_info
    )


def _update_path_fingerprint(
    digest: DigestWriter, project: Path, raw: bytes, relative: str
) -> None:
    path = project / relative
    digest.update(raw + b'\0')
    if path.is_symlink():
        digest.update(b'LINK\0' + os.fsencode(os.readlink(path)))
    elif path.is_file():
        digest.update(b'FILE\0' + file_digest(path).encode())
    elif not path.exists():
        digest.update(b'DELETED')
    else:
        digest.update(b'OTHER')
    digest.update(b'\0')


def fingerprint(project: Path) -> str:
    """Digest tracked and untracked non-ignored files, including deletes and file names."""
    proc = subprocess.run(
        ['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'],
        cwd=project,
        capture_output=True,
        check=True,
    )
    paths = sorted(set(filter(None, proc.stdout.split(b'\0'))))
    digest = hashlib.sha256()
    for raw in paths:
        relative = os.fsdecode(raw)
        if _is_generated(relative):
            continue
        _update_path_fingerprint(digest, project, raw, relative)
    return digest.hexdigest()
