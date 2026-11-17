"""Coverage generation and strict LCOV integrity validation."""

from __future__ import annotations

import ast
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path

from .errors import GateError
from .execution import CommandRunner
from .sources import discover_python_files

PytestBuilder = Callable[..., list[str]]


def _files_for_source(project: Path, root: Path, discovered: set[Path], source: str) -> set[Path]:
    target = project / source
    resolved = target.resolve()
    if resolved != root and root not in resolved.parents:
        raise GateError(f'INVALID_COVERAGE_SOURCE: path outside project: {target}')
    relative = target.relative_to(project)
    if target.is_file() and not target.is_symlink():
        return {relative} if relative in discovered else set()
    if target.is_dir() and not target.is_symlink():
        return {path for path in discovered if path.is_relative_to(relative)}
    return set()


def coverage_source_files(project: Path, sources: list[str]) -> list[Path]:
    """Expand approved source targets to project-owned Python files."""
    root = project.resolve()
    discovered = {Path(path) for path in discover_python_files(project)}
    files: set[Path] = set()
    for source in sources:
        files.update(_files_for_source(project, root, discovered, source))
    if not files:
        raise GateError('INVALID_COVERAGE_SOURCE: no Python files in selected sources')
    return sorted(files)


def _lcov_facts(project: Path, report: Path) -> dict[Path, int]:
    line_data: dict[Path, int] = {}
    current: Path | None = None
    try:
        lines = report.read_text().splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise GateError('INVALID_COVERAGE_ARTIFACT: LCOV невозможно прочитать') from exc
    for line in lines:
        if line.startswith('SF:'):
            source = Path(line[3:])
            absolute = source if source.is_absolute() else project / source
            current = absolute.resolve()
            line_data.setdefault(current, 0)
        elif current is not None and re.fullmatch(r'DA:\d+,\d+(?:,.*)?', line):
            line_data[current] += 1
        elif line == 'end_of_record':
            current = None
    return line_data


def _has_executable_code(path: Path) -> bool:
    try:
        module = ast.parse(path.read_text(), filename=str(path))
    except (OSError, UnicodeDecodeError, SyntaxError) as exc:
        raise GateError(f'INVALID_COVERAGE_SOURCE: невозможно разобрать {path}') from exc
    body = module.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        if isinstance(body[0].value.value, str):
            body = body[1:]
    return bool(body)


def _paths_text(paths: set[Path]) -> str:
    return ', '.join(sorted(str(path) for path in paths))


def validate_lcov(project: Path, report: Path, sources: list[str]) -> None:
    """Require line data exclusively for the source files selected by the gate."""
    if not report.is_file() or report.stat().st_size == 0:
        raise GateError('MISSING_COVERAGE_ARTIFACT: LCOV отсутствует или пуст')
    allowed = {(project / path).resolve() for path in coverage_source_files(project, sources)}
    line_data = _lcov_facts(project, report)
    covered = set(line_data)
    if not covered:
        raise GateError('INVALID_COVERAGE_ARTIFACT: LCOV не содержит фактических line data')
    unexpected = covered - allowed
    if unexpected:
        raise GateError(
            f'INVALID_COVERAGE_ARTIFACT: посторонние source files: {_paths_text(unexpected)}'
        )
    missing = allowed - covered
    if missing:
        raise GateError(
            f'INVALID_COVERAGE_ARTIFACT: отсутствуют source files: {_paths_text(missing)}'
        )
    without_data = {path for path in allowed if line_data[path] == 0 and _has_executable_code(path)}
    if without_data:
        raise GateError(
            'INVALID_COVERAGE_ARTIFACT: исполняемые source files не содержат DA: '
            + _paths_text(without_data)
        )


def generate_python_coverage(
    project: Path,
    backend: list[str],
    python: list[str],
    sources: list[str],
    report: Path,
    *,
    executor: CommandRunner,
    python_invocation: Callable[..., list[str]],
    pytest_builder: PytestBuilder,
) -> None:
    """Generate, validate, and atomically publish fresh project-scoped LCOV."""
    report.parent.mkdir(parents=True, exist_ok=True)
    if report.exists():
        report.unlink()
    included = coverage_source_files(project, sources)
    with tempfile.TemporaryDirectory(prefix='.python-coverage-', dir=report.parent) as temp:
        scratch = Path(temp)
        data = scratch / '.coverage'
        candidate = scratch / 'lcov.info'
        config = scratch / 'coverage.ini'
        config.write_text('[run]\npatch =\n    subprocess\n')
        env = os.environ.copy()
        env['COVERAGE_FILE'] = str(data)
        env['COVERAGE_PROCESS_START'] = str(config)
        executor(
            project,
            pytest_builder(
                backend,
                '-q',
                'tests',
                '--cov=.',
                '--cov-report=',
                f'--cov-config={config}',
            ),
            env=env,
        )
        executor(
            project,
            python_invocation(
                python,
                project,
                '-m',
                'coverage',
                'lcov',
                f'--data-file={data}',
                '--include=' + ','.join(path.as_posix() for path in included),
                '-o',
                str(candidate),
            ),
        )
        validate_lcov(project, candidate, sources)
        os.replace(candidate, report)
