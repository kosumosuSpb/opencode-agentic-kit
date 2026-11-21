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
    re.IGNORECASE | re.MULTILINE,
)


def is_canonical_criterion(identifier: str) -> bool:
    return _CANONICAL_CRITERION.fullmatch(identifier) is not None


def validate_criterion_definitions(text: str, label: str) -> None:
    """Reject malformed IDs only where a Markdown line defines a criterion."""
    for match in _CRITERION_DEFINITION.finditer(text):
        identifier = match.group('identifier')
        if is_canonical_criterion(identifier):
            continue
        line = text.count('\n', 0, match.start('identifier')) + 1
        raise GateError(
            f'INVALID_ACCEPTANCE_CRITERION: {label}:{line}: {identifier}; '
            f'expected {CRITERION_FORMAT}'
        )
