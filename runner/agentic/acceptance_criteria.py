"""Canonical acceptance-criterion identifiers and draft validation."""

from __future__ import annotations

import re

from .errors import GateError

CRITERION_FORMAT = r'AC[1-9][0-9]{0,3}'
_CANONICAL_CRITERION = re.compile(CRITERION_FORMAT)
_CRITERION_DEFINITION = re.compile(
    r'^[ \t]*(?:(?:#{1,6}|[-+*]|\d+[.)]|\|)[ \t]+)?'
    r'(?:\[[ xX]\][ \t]+)?(?:\*\*|__|`)?'
    r'(?P<identifier>AC(?:[-_]?\d+))'
    r'(?:(?:\*\*|__|`)[ \t]*[:–—|]|[ \t]*[:–—|][ \t]*(?:\*\*|__|`)?)',
    re.IGNORECASE,
)
# CommonMark fenced blocks: up to three leading spaces and a fence of >=3
# matching backticks or tildes. Contents are examples, not AC definitions.
_FENCE_OPEN = re.compile(r'^[ ]{0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$')
_FENCE_CLOSE = re.compile(r'^[ ]{0,3}(?P<fence>`{3,}|~{3,})[ \t]*$')


def is_canonical_criterion(identifier: str) -> bool:
    return _CANONICAL_CRITERION.fullmatch(identifier) is not None


def validate_criterion_definitions(text: str, label: str) -> None:
    """Reject malformed IDs only where a Markdown line defines a criterion."""
    active_fence: str | None = None
    for line_number, line in enumerate(text.splitlines(), start=1):
        if active_fence is not None:
            closing = _FENCE_CLOSE.fullmatch(line)
            if (closing is not None
                    and closing.group('fence')[0] == active_fence[0]
                    and len(closing.group('fence')) >= len(active_fence)):
                active_fence = None
            continue

        opening = _FENCE_OPEN.fullmatch(line)
        if opening is not None:
            fence = opening.group('fence')
            # A backtick fence cannot have a backtick in its info string.
            if fence[0] != '`' or '`' not in opening.group('info'):
                active_fence = fence
                continue

        match = _CRITERION_DEFINITION.match(line)
        if match is None:
            continue
        identifier = match.group('identifier')
        if is_canonical_criterion(identifier):
            continue
        raise GateError(
            f'INVALID_ACCEPTANCE_CRITERION: {label}:{line_number}: {identifier}; '
            f'expected {CRITERION_FORMAT}'
        )
