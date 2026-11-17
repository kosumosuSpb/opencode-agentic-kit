"""Isolated quality-gate execution with a per-run command journal."""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .approval import ApprovalService
from .coverage import generate_python_coverage
from .errors import GateError, GateFailure
from .execution import CommandRunner
from .project import file_digest
from .sources import source_targets
from .tasks import TaskService
from .wheel import WheelVerifier, wheel_applicability

SourceDiscovery = Callable[[Path], tuple[list[str], list[str]]]
ToolResolver = Callable[[str], Path]
BackendSelector = Callable[[Path], tuple[list[str], list[str]]]
ModuleChecker = Callable[[Path, list[str], str], None]
CoverageGenerator = Callable[[Path, list[str], list[str], list[str], Path], None]
WheelApplicability = Callable[[Path], dict[str, object]]
WheelCheck = Callable[[Path, list[str]], dict[str, object]]


def execute_command(
    project: Path,
    command: list[str],
    *,
    capture: bool = False,
    env: dict[str, str] | None = None,
    echo_capture: bool = True,
) -> str:
    print(f'$ (cd {shlex.quote(str(project))} && {shlex.join(command)})', flush=True)
    try:
        done = subprocess.run(
            command,
            cwd=project,
            text=True,
            env=env,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.STDOUT if capture else None,
            check=False,
        )
    except FileNotFoundError as exc:
        raise GateError(f'MISSING_EXECUTABLE: {command[0]}') from exc
    if capture and echo_capture:
        print(done.stdout, end='', flush=True)
    if done.returncode:
        raise GateFailure(f'COMMAND_FAILED(exit={done.returncode}): {" ".join(command)}')
    return done.stdout if capture else ''


class CommandJournal:
    """Record commands for exactly one QualityRunner execution."""

    def __init__(self, delegate: CommandRunner = execute_command) -> None:
        self._delegate = delegate
        self._entries: list[dict[str, object]] = []

    @property
    def entries(self) -> list[dict[str, object]]:
        """Mutable journal used only by the owning runner and wheel verifier."""
        return self._entries

    def snapshot(self) -> list[dict[str, object]]:
        return [dict(entry) for entry in self._entries]

    def run(
        self,
        project: Path,
        command: list[str],
        *,
        capture: bool = False,
        env: dict[str, str] | None = None,
        echo_capture: bool = True,
    ) -> str:
        return self(
            project,
            command,
            capture=capture,
            env=env,
            echo_capture=echo_capture,
        )

    def __call__(
        self,
        project: Path,
        command: list[str],
        *,
        capture: bool = False,
        env: dict[str, str] | None = None,
        echo_capture: bool = True,
    ) -> str:
        try:
            output = self._delegate(
                project,
                command,
                capture=capture,
                env=env,
                echo_capture=echo_capture,
            )
        except GateFailure as exc:
            self._append(project, command, _failure_exit_code(exc), capture, '')
            raise
        output = output or ''
        self._append(project, command, 0, capture, output)
        return output

    def _append(
        self,
        project: Path,
        command: list[str],
        exit_code: int,
        capture: bool,
        output: str,
    ) -> None:
        self._entries.append(
            {
                'cwd': str(project),
                'command': command,
                'exit_code': exit_code,
                **({'output': output[-4000:]} if capture else {}),
            }
        )


def _failure_exit_code(error: GateFailure) -> int:
    match = re.search(r'COMMAND_FAILED\(exit=(\d+)\)', str(error))
    return int(match.group(1)) if match else 1


def python_invocation(python: list[str], project: Path, *args: str) -> list[str]:
    if python[:3] == ['uv', 'run', '--locked']:
        return ['uv', 'run', '--project', str(project), '--locked', 'python', *args]
    return [python[0], *args]


def python_backend(project: Path) -> tuple[list[str], list[str]]:
    if (project / 'uv.lock').is_file():
        if not shutil.which('uv'):
            raise GateError('MISSING_QUALITY_TOOL: uv недоступен')
        return ['uv', 'run', '--locked'], ['uv', 'run', '--locked']
    candidate = project / '.venv/bin/python'
    interpreter = str(candidate) if candidate.is_file() else sys.executable
    return [interpreter, '-m'], [interpreter]


def require_module(project: Path, runner: list[str], module: str) -> None:
    if runner[:3] == ['uv', 'run', '--locked']:
        return
    proc = subprocess.run(
        [
            runner[0],
            '-c',
            f'import importlib.util; assert importlib.util.find_spec({module!r}) is not None',
        ],
        cwd=project,
        capture_output=True,
        check=False,
    )
    if proc.returncode:
        raise GateError(f'MISSING_QUALITY_TOOL: Python module {module}')


def pytest_command(backend: list[str], *args: str) -> list[str]:
    return [*backend, 'pytest', *args]


def default_tool_path(name: str) -> Path:
    tools = Path(
        os.environ.get(
            'AGENTIC_TOOLS_DIR', str(Path.home() / '.local/share/opencode-agentic/tools')
        )
    )
    executable = tools / name / name
    if not executable.is_file():
        raise GateError(f'MISSING_QUALITY_TOOL: {executable}. Запусти bootstrap-tools.sh')
    return executable


def mutator_command(
    project: Path, backend: list[str], sources: list[str], tool_resolver: ToolResolver
) -> list[str]:
    test = shlex.join(pytest_command(backend, '-q', 'tests'))
    return [
        str(tool_resolver('mutator')),
        '--use-existing-coverage',
        '--test-command',
        test,
        '--max-workers',
        '2',
        *sources,
    ]


@dataclass(frozen=True)
class QualityRunResult:
    wheel: dict[str, object]
    commands: list[dict[str, object]]


class QualityRunner:
    """Run one FAST/VERIFY/FULL sequence with explicitly owned dependencies."""

    def __init__(
        self,
        project: Path,
        tasks: TaskService,
        approval: ApprovalService,
        journal: CommandJournal,
        *,
        discover_sources: SourceDiscovery = source_targets,
        select_backend: BackendSelector = python_backend,
        check_module: ModuleChecker = require_module,
        resolve_tool: ToolResolver = default_tool_path,
        coverage_generator: CoverageGenerator | None = None,
        wheel_applicability_check: WheelApplicability = wheel_applicability,
        wheel_check: WheelCheck | None = None,
    ) -> None:
        self.project = project
        self.tasks = tasks
        self.approval = approval
        self.journal = journal
        self._discover_sources = discover_sources
        self._select_backend = select_backend
        self._check_module = check_module
        self._resolve_tool = resolve_tool
        self._coverage_generator = coverage_generator
        self._wheel_applicability = wheel_applicability_check
        self._wheel_check = wheel_check

    def run(self, level: str) -> QualityRunResult:
        print(f'=== QUALITY GATE: {level.upper()} ===')
        self.approval.validate_baseline()
        sources, lint = self._discover_sources(self.project)
        backend, python = self._select_backend(self.project)
        module_command = self._module_command(backend, level)
        self._run_static_checks(module_command, backend, python, sources, lint)
        wheel = self._level_checks(level, module_command, backend, python, sources)
        self.approval.validate_baseline()
        print(f'=== QUALITY GATE {level.upper()}: PASS ===')
        return QualityRunResult(wheel=wheel, commands=self.journal.snapshot())

    def _module_command(self, backend: list[str], level: str) -> Callable[[str, str], list[str]]:
        if backend != ['uv', 'run', '--locked']:
            modules = ['ruff', 'mypy', 'pytest']
            if level != 'fast':
                modules.extend(('pytest_cov', 'coverage'))
            for module in modules:
                self._check_module(self.project, [backend[0]], module)
        return lambda name, *args: [*backend, name, *args]

    def _run_static_checks(
        self,
        module_command: Callable[..., list[str]],
        backend: list[str],
        python: list[str],
        sources: list[str],
        lint: list[str],
    ) -> None:
        version = (
            [*python, 'python', '--version']
            if python[:3] == ['uv', 'run', '--locked']
            else [python[0], '--version']
        )
        self.journal(self.project, version, capture=True)
        for tool in ('ruff', 'mypy', 'pytest'):
            self.journal(self.project, module_command(tool, '--version'), capture=True)
        self.journal(self.project, module_command('ruff', 'check', *lint))
        self.journal(self.project, module_command('ruff', 'format', '--check', *lint))
        self.journal(self.project, module_command('mypy', *sources))

    def _level_checks(
        self,
        level: str,
        module_command: Callable[..., list[str]],
        backend: list[str],
        python: list[str],
        sources: list[str],
    ) -> dict[str, object]:
        if level == 'fast':
            self.journal(self.project, module_command('pytest', '-q', 'tests'))
            return {
                'applicable': None,
                'status': 'not_run',
                'reason': 'FAST does not run wheel verification',
            }
        self._coverage(backend, python, sources)
        self._managed_quality(level, backend, sources)
        return self._wheel(python)

    def _coverage(self, backend: list[str], python: list[str], sources: list[str]) -> None:
        report = self.project / 'target/coverage/python/lcov.info'
        if self._coverage_generator is not None:
            self._coverage_generator(self.project, backend, python, sources, report)
            return
        generate_python_coverage(
            self.project,
            backend,
            python,
            sources,
            report,
            executor=self.journal,
            python_invocation=python_invocation,
            pytest_builder=pytest_command,
        )

    def _managed_quality(self, level: str, backend: list[str], sources: list[str]) -> None:
        self.journal(
            self.project,
            [
                str(self._resolve_tool('crapper')),
                '--use-existing-coverage',
                '--threshold',
                '10',
                *sources,
            ],
        )
        duplicates = self.journal(
            self.project,
            [str(self._resolve_tool('dryer')), '--threshold', '0.8', *sources],
            capture=True,
        )
        if re.search(r'^DUPLICATE\s+score=', duplicates, re.MULTILINE):
            raise GateFailure('DRYER_DUPLICATES: дублирование выше 0.8; требуется разбор')
        if level == 'full':
            self.journal(
                self.project,
                mutator_command(self.project, backend, sources, self._resolve_tool),
            )

    def _wheel(self, python: list[str]) -> dict[str, object]:
        applicability = self._wheel_applicability(self.project)
        if applicability['applicable'] is not True:
            wheel = {**applicability, 'status': 'not_applicable'}
            print(f'=== WHEEL EVIDENCE: NOT APPLICABLE ({wheel["reason"]}) ===')
            return wheel
        if self._wheel_check is not None:
            return self._wheel_check(self.project, python)
        verifier = WheelVerifier(self.journal, self.journal.entries, python_invocation, file_digest)
        return verifier.verify(self.project, python)


def baseline_test(
    project: Path,
    tasks: TaskService,
    approval: ApprovalService,
    journal: CommandJournal,
    select_backend: BackendSelector = python_backend,
    check_module: ModuleChecker = require_module,
) -> None:
    tasks.require_open()
    approval.validate_baseline()
    backend, python = select_backend(project)
    if backend != ['uv', 'run', '--locked']:
        check_module(project, python, 'pytest')
    journal(project, pytest_command(backend, '-q', 'tests'))
