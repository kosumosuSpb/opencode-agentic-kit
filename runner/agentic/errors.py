"""Runner error taxonomy shared by application components."""

from __future__ import annotations


class GateError(Exception):
    """The Runner cannot establish a valid quality result."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.wheel: dict[str, object] | None = None


class GateFailure(GateError):
    """A quality check ran successfully and reported defects."""
