"""Stable executable entrypoint for the Agentic Kit Runner."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from .agentic.cli import main
else:
    from agentic.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
