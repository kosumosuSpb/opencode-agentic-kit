"""Shared execution boundary types for independently testable Runner components."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class CommandRunner(Protocol):
    def __call__(
        self,
        project: Path,
        command: list[str],
        *,
        capture: bool = False,
        env: dict[str, str] | None = None,
        echo_capture: bool = True,
    ) -> str: ...
