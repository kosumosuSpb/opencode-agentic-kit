"""Atomic JSON persistence and managed path validation."""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from .errors import GateError


@contextmanager
def exclusive_file_lock(path: Path, error_code: str) -> Iterator[None]:
    """Serialize processes that mutate one persisted state boundary."""
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise GateError(f'{error_code}: cannot open {path}') from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except OSError as exc:
            raise GateError(f'{error_code}: cannot lock {path}') from exc
        yield
    finally:
        os.close(descriptor)


class StatefulService:
    """Shared immutable dependencies for persisted lifecycle services."""

    def __init__(
        self,
        project: Path,
        storage: JsonStorage,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.project = project
        self.storage = storage
        self._clock = clock or (lambda: datetime.now(UTC))


class JsonStorage:
    """Stateless adapter for durable JSON objects."""

    def write(self, path: Path, data: object) -> None:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(data, indent=2) + '\n')
        os.replace(temporary, path)

    def read_object(self, path: Path, error: str) -> dict[str, object]:
        try:
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise TypeError
            return value
        except (OSError, ValueError, TypeError) as exc:
            raise GateError(error) from exc

    def reject_symlink_ancestors(self, path: Path, root: Path) -> None:
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
