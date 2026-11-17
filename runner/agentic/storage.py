"""Atomic JSON persistence and managed path validation."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from .errors import GateError


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
