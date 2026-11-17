"""Wheel applicability and isolated artifact verification."""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import shutil
import tempfile
import tomllib
import zipfile
from collections.abc import Callable
from pathlib import Path

from .errors import GateError
from .execution import CommandRunner


def _declared_import_names(archive: zipfile.ZipFile, names: list[str]) -> set[str]:
    declared = [name for name in names if name.endswith('.dist-info/top_level.txt')]
    return {
        line.strip()
        for name in declared
        for line in archive.read(name).decode('utf-8').splitlines()
    }


def _inferred_import_names(names: list[str]) -> set[str]:
    candidates: set[str] = set()
    for name in names:
        parts = name.split('/')
        if not parts or parts[0].endswith(('.dist-info', '.data')):
            continue
        top = parts[0].removesuffix('.py')
        if top.isidentifier():
            candidates.add(top)
    return candidates


def _pyproject_applicability(pyproject: Path) -> dict[str, object] | None:
    if not pyproject.is_file():
        return None
    try:
        metadata = tomllib.loads(pyproject.read_text())
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise GateError('INVALID_PACKAGING_METADATA: cannot parse pyproject.toml') from exc
    if isinstance(metadata.get('build-system'), dict):
        return {'applicable': True, 'reason': 'pyproject.toml declares build-system'}
    if isinstance(metadata.get('project'), dict):
        return {'applicable': True, 'reason': 'pyproject.toml declares project metadata'}
    return {'applicable': False, 'reason': 'pyproject.toml contains tool configuration only'}


def _setup_cfg_applicable(setup_cfg: Path) -> bool:
    if not setup_cfg.is_file():
        return False
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read_string(setup_cfg.read_text())
    except (OSError, UnicodeDecodeError, configparser.Error) as exc:
        raise GateError('INVALID_PACKAGING_METADATA: cannot parse setup.cfg') from exc
    return parser.has_section('metadata') and parser.has_option('metadata', 'name')


def wheel_applicability(project: Path) -> dict[str, object]:
    """Identify distributable Python projects without conflating them with scripts."""
    pyproject = _pyproject_applicability(project / 'pyproject.toml')
    if pyproject and pyproject['applicable'] is True:
        return pyproject
    if (project / 'setup.py').is_file():
        return {'applicable': True, 'reason': 'legacy setup.py packaging metadata'}
    if _setup_cfg_applicable(project / 'setup.cfg'):
        return {'applicable': True, 'reason': 'legacy setup.cfg packaging metadata'}
    if pyproject:
        return pyproject
    return {'applicable': False, 'reason': 'no supported Python packaging metadata'}


def wheel_import_names(wheel: Path) -> list[str]:
    """Discover importable top-level names from the built artifact itself."""
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            candidates = _declared_import_names(archive, names)
            if not candidates:
                candidates = _inferred_import_names(names)
    except (OSError, zipfile.BadZipFile, UnicodeDecodeError) as exc:
        raise GateError('INVALID_WHEEL: cannot inspect built artifact') from exc
    imports = sorted(name for name in candidates if name.isidentifier())
    if not imports:
        raise GateError('WHEEL_NO_IMPORTABLE_MODULES: artifact exposes no Python API')
    return imports


class WheelVerifier:
    """Build and verify one wheel using an explicitly owned execution journal."""

    def __init__(
        self,
        executor: CommandRunner,
        command_log: list[dict[str, object]],
        python_invocation: Callable[..., list[str]],
        digest_file: Callable[[Path], str],
    ) -> None:
        self._run = executor
        self._command_log = command_log
        self._python_invocation = python_invocation
        self._digest_file = digest_file

    def _annotate_last_command(self, start: int, **metadata: object) -> None:
        if len(self._command_log) > start:
            self._command_log[-1].update(metadata)

    def _verify_import_provenance(
        self,
        project: Path,
        python: list[str],
        scratch: Path,
        site: Path,
        wheel: Path,
        isolated_env: dict[str, str],
    ) -> list[str]:
        imports = wheel_import_names(wheel)
        provenance = r"""
import importlib, json, pathlib, sys
site = pathlib.Path(sys.argv[1]).resolve()
source = pathlib.Path(sys.argv[2]).resolve()
names = json.loads(sys.argv[3])
kept = []
for entry in sys.path:
    if not entry:
        continue
    resolved = pathlib.Path(entry).resolve()
    if resolved == source or source in resolved.parents:
        continue
    kept.append(entry)
sys.path[:] = [str(site), *kept]
result = {}
for name in names:
    module = importlib.import_module(name)
    origins = []
    filename = getattr(module, '__file__', None)
    if filename:
        origins.append(pathlib.Path(filename).resolve())
    module_path = getattr(module, '__path__', None)
    if module_path:
        origins.extend(pathlib.Path(item).resolve() for item in module_path)
    if not origins or any(origin != site and site not in origin.parents for origin in origins):
        raise RuntimeError(f'{name} imported outside wheel target: {origins}')
    result[name] = [str(origin) for origin in origins]
print(json.dumps({'imports': result, 'source_root_excluded': True}, sort_keys=True))
""".strip()
        before = len(self._command_log)
        self._run(
            scratch,
            self._python_invocation(
                python,
                project,
                '-I',
                '-S',
                '-c',
                provenance,
                str(site),
                str(project),
                json.dumps(imports),
            ),
            env=isolated_env,
            capture=True,
        )
        self._annotate_last_command(
            before,
            kind='wheel_import_provenance',
            imports=imports,
            source_root=str(project),
            source_root_excluded=True,
            target=str(site),
        )
        return imports

    def _copy_test_toolchain(
        self,
        project: Path,
        python: list[str],
        scratch: Path,
        isolated_env: dict[str, str],
    ) -> Path:
        probe = r"""
import importlib.metadata as metadata, json, pathlib, re, sysconfig
from packaging.requirements import Requirement
purelib = pathlib.Path(sysconfig.get_paths()['purelib']).resolve()
queue = ['pytest']
seen = set()
files = set()
while queue:
    name = queue.pop()
    key = re.sub(r'[-_.]+', '-', name).lower()
    if key in seen:
        continue
    seen.add(key)
    try:
        distribution = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        continue
    for item in distribution.files or ():
        source = pathlib.Path(distribution.locate_file(item)).resolve()
        if source.is_file() and (source == purelib or purelib in source.parents):
            files.add(str(source.relative_to(purelib)))
    for raw_requirement in distribution.requires or ():
        requirement = Requirement(raw_requirement)
        if requirement.marker and not requirement.marker.evaluate({'extra': ''}):
            continue
        queue.append(requirement.name)
print(json.dumps({'purelib': str(purelib), 'tool_files': sorted(files)}))
""".strip()
        probe_start = len(self._command_log)
        raw = self._run(
            scratch,
            self._python_invocation(python, project, '-I', '-c', probe),
            env=isolated_env,
            capture=True,
            echo_capture=False,
        )
        try:
            tool_paths = json.loads(raw.strip().splitlines()[-1])
            purelib = Path(tool_paths['purelib']).resolve()
            tool_files = tool_paths.get('tool_files', [])
            if not isinstance(tool_files, list) or not all(
                isinstance(item, str) for item in tool_files
            ):
                raise TypeError
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise GateError('PYTHON_TOOL_PATHS_UNRESOLVED: cannot locate isolated pytest') from exc
        if len(self._command_log) > probe_start:
            self._command_log[-1].update(
                {
                    'kind': 'wheel_test_toolchain',
                    'probe_output_sha256': hashlib.sha256(raw.encode()).hexdigest(),
                    'tool_file_count': len(tool_files),
                    'output': json.dumps(
                        {'purelib': str(purelib), 'tool_file_count': len(tool_files)}
                    ),
                }
            )
        toolsite = scratch / 'test-tools'
        toolsite.mkdir()
        for relative in tool_files:
            source = (purelib / relative).resolve()
            try:
                source.relative_to(purelib)
            except ValueError as exc:
                raise GateError(f'PYTHON_TOOL_PATH_ESCAPE: {relative}') from exc
            if not source.is_file():
                raise GateError(f'PYTHON_TOOL_FILE_MISSING: {source}')
            destination = toolsite / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        return toolsite

    def _verify_tests(
        self,
        project: Path,
        python: list[str],
        scratch: Path,
        site: Path,
        smoke_tests: Path,
        toolsite: Path,
        imports: list[str],
        isolated_env: dict[str, str],
    ) -> None:
        smoke = r"""
import pathlib, sys
site = pathlib.Path(sys.argv[1]).resolve()
tests = pathlib.Path(sys.argv[2]).resolve()
source = pathlib.Path(sys.argv[3]).resolve()
toolsite = pathlib.Path(sys.argv[4]).resolve()
kept = []
for entry in sys.path:
    if not entry:
        continue
    resolved = pathlib.Path(entry).resolve()
    if resolved == source or source in resolved.parents:
        continue
    kept.append(entry)
sys.path[:] = [str(site), *kept, str(toolsite)]
if any((resolved := pathlib.Path(entry).resolve()) == source or source in resolved.parents
       for entry in sys.path if entry):
    raise RuntimeError('source project leaked into isolated sys.path')
import pytest
raise SystemExit(pytest.main(['-q', str(tests), '-c', '/dev/null']))
""".strip()
        before = len(self._command_log)
        self._run(
            scratch,
            self._python_invocation(
                python,
                project,
                '-I',
                '-S',
                '-c',
                smoke,
                str(site),
                str(smoke_tests),
                str(project),
                str(toolsite),
            ),
            env=isolated_env,
            capture=True,
        )
        self._annotate_last_command(
            before,
            kind='wheel_public_api_smoke',
            tests=str(smoke_tests),
            source_root=str(project),
            source_root_excluded=True,
            target=str(site),
            import_names=imports,
        )

    def _isolated_checks(
        self, project: Path, python: list[str], scratch: Path, site: Path, wheel: Path
    ) -> None:
        isolated_env = {
            key: value
            for key, value in os.environ.items()
            if key not in {'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP'}
        }
        isolated_env['PYTHONNOUSERSITE'] = '1'
        imports = self._verify_import_provenance(
            project, python, scratch, site, wheel, isolated_env
        )
        tests = project / 'tests'
        for candidate in tests.rglob('*'):
            if candidate.is_symlink():
                raise GateError(
                    f'WHEEL_SMOKE_SYMLINK: tests cannot escape source tree: {candidate}'
                )
        smoke_tests = scratch / 'tests'
        shutil.copytree(tests, smoke_tests)
        if (project / 'pyproject.toml').is_file():
            shutil.copy2(project / 'pyproject.toml', scratch / 'pyproject.toml')
        toolsite = self._copy_test_toolchain(project, python, scratch, isolated_env)
        self._verify_tests(
            project,
            python,
            scratch,
            site,
            smoke_tests,
            toolsite,
            imports,
            isolated_env,
        )

    def verify_installed_artifact(
        self, project: Path, python: list[str], scratch: Path, site: Path, wheel: Path
    ) -> None:
        """Verify an already installed wheel target without consulting the source import path."""
        self._isolated_checks(project, python, scratch, site, wheel)

    def _build(self, project: Path, python: list[str], scratch: Path, dist: Path) -> None:
        if python[:3] == ['uv', 'run', '--locked']:
            command = [
                'uv',
                'build',
                '--wheel',
                '--no-build-isolation',
                '--offline',
                '--no-python-downloads',
                '--out-dir',
                str(dist),
                str(project),
            ]
        else:
            command = [
                python[0],
                '-m',
                'pip',
                'wheel',
                '--no-deps',
                '--no-build-isolation',
                '--wheel-dir',
                str(dist),
                str(project),
            ]
        self._run(scratch, command, capture=True)

    def _install(self, python: list[str], scratch: Path, site: Path, wheel: Path) -> None:
        if python[:3] == ['uv', 'run', '--locked']:
            command = [
                'uv',
                'pip',
                'install',
                '--offline',
                '--target',
                str(site),
                '--no-deps',
                str(wheel),
            ]
        else:
            command = [
                python[0],
                '-m',
                'pip',
                'install',
                '--no-deps',
                '--target',
                str(site),
                str(wheel),
            ]
        self._run(scratch, command, capture=True)

    def verify(self, project: Path, python: list[str]) -> dict[str, object]:
        """Build, install, and exercise only the wheel, never the source checkout."""
        applicability = wheel_applicability(project)
        if applicability['applicable'] is not True:
            raise GateError('WHEEL_NOT_APPLICABLE: project has no packaging metadata')
        try:
            with tempfile.TemporaryDirectory(prefix='agentic-wheel-') as raw:
                scratch = Path(raw)
                dist = scratch / 'dist'
                site = scratch / 'site'
                dist.mkdir()
                site.mkdir()
                self._build(project, python, scratch, dist)
                wheels = list(dist.glob('*.whl'))
                if len(wheels) != 1:
                    raise GateError(f'WHEEL_COUNT: expected one wheel, got {len(wheels)}')
                wheel = wheels[0]
                self._command_log.append(
                    {
                        'kind': 'wheel_artifact',
                        'path': wheel.name,
                        'sha256': self._digest_file(wheel),
                    }
                )
                self._install(python, scratch, site, wheel)
                self._isolated_checks(project, python, scratch, site, wheel)
        except GateError as exc:
            exc.wheel = {'applicable': True, 'status': 'failed', 'reason': str(exc)}
            raise
        print('=== WHEEL EVIDENCE: PASS (artifact provenance and public API smoke isolated) ===')
        return {
            'applicable': True,
            'status': 'verified',
            'reason': 'wheel build, install and isolated smoke passed',
        }
