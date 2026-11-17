from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from runner.agentic.coverage import coverage_source_files, validate_lcov
from runner.agentic.errors import GateError, GateFailure
from runner.agentic.project import identity, state_dir, validate_git_root
from runner.agentic.sources import discover_python_files, source_targets
from runner.agentic.wheel import wheel_applicability, wheel_import_names


class ErrorContractTests(unittest.TestCase):
    def test_gate_failure_is_a_ran_check_failure_with_isolated_evidence(self):
        first = GateFailure('failed check')
        second = GateError('blocked check')

        first.wheel = {'status': 'failed'}

        self.assertIsInstance(first, GateError)
        self.assertEqual(first.wheel, {'status': 'failed'})
        self.assertIsNone(second.wheel)


class ProjectContractTests(unittest.TestCase):
    def test_identity_and_state_path_are_stable_for_resolved_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / 'project'
            project.mkdir()
            state = Path(tmp) / 'state'
            with patch.dict(os.environ, {'AGENTIC_STATE_DIR': str(state)}):
                first = state_dir(project)
                second = state_dir(project / '.')

        self.assertEqual(first, second)
        self.assertEqual(first, state / 'projects' / identity(project))

    def test_validate_git_root_rejects_nested_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / 'project'
            nested = project / 'nested'
            nested.mkdir(parents=True)
            subprocess.run(['git', 'init', '-q', str(project)], check=True)

            validate_git_root(project)
            with self.assertRaisesRegex(GateError, 'PROJECT_NOT_GIT_ROOT'):
                validate_git_root(nested)


class SourceContractTests(unittest.TestCase):
    def test_discovery_is_bounded_and_classifies_tests_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / 'pkg').mkdir()
            (project / 'pkg/__init__.py').write_text('VALUE = 1\n')
            (project / 'tests').mkdir()
            (project / 'tests/test_pkg.py').write_text('def test_value(): pass\n')
            outside = project.parent / f'{project.name}-outside.py'
            outside.write_text('ESCAPED = True\n')
            self.addCleanup(outside.unlink, missing_ok=True)
            try:
                (project / 'linked.py').symlink_to(outside)
            except OSError as exc:
                self.skipTest(f'symlinks unavailable: {exc}')

            discovered = discover_python_files(project)
            production, lint = source_targets(project)

        self.assertEqual(discovered, ['pkg/__init__.py', 'tests/test_pkg.py'])
        self.assertEqual(production, ['pkg/__init__.py'])
        self.assertEqual(lint, discovered)


class CoverageContractTests(unittest.TestCase):
    def test_lcov_must_cover_every_selected_source_and_no_other_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / 'pkg').mkdir()
            (project / 'pkg/a.py').write_text('A = 1\n')
            (project / 'pkg/b.py').write_text('B = 2\n')
            (project / 'tests').mkdir()
            (project / 'tests/test_pkg.py').write_text('def test_pkg(): pass\n')
            report = project / 'lcov.info'
            report.write_text(f'SF:{project / "pkg/a.py"}\nDA:1,1\nend_of_record\n')

            self.assertEqual(
                coverage_source_files(project, ['pkg']),
                [Path('pkg/a.py'), Path('pkg/b.py')],
            )
            with self.assertRaisesRegex(GateError, 'отсутствуют source files'):
                validate_lcov(project, report, ['pkg'])

    def test_lcov_rejects_executable_source_record_without_da_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / 'pkg').mkdir()
            (project / 'pkg/a.py').write_text('A = 1\n')
            (project / 'pkg/b.py').write_text('B = 2\n')
            report = project / 'lcov.info'
            report.write_text(
                'SF:pkg/a.py\nDA:1,1\nend_of_record\nSF:pkg/b.py\nLF:1\nLH:0\nend_of_record\n'
            )

            with self.assertRaisesRegex(GateError, 'не содержат DA'):
                validate_lcov(project, report, ['pkg'])

    def test_lcov_allows_empty_init_without_da_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / 'pkg').mkdir()
            (project / 'pkg/__init__.py').write_text('')
            (project / 'pkg/module.py').write_text('VALUE = 1\n')
            report = project / 'lcov.info'
            report.write_text(
                'SF:pkg/__init__.py\nLF:0\nLH:0\nend_of_record\n'
                'SF:pkg/module.py\nDA:1,1\nend_of_record\n'
            )

            validate_lcov(project, report, ['pkg'])


class WheelContractTests(unittest.TestCase):
    def test_import_names_are_inferred_from_artifact_without_declared_top_level(self):
        with tempfile.TemporaryDirectory() as tmp:
            artifact = Path(tmp) / 'sample.whl'
            with zipfile.ZipFile(artifact, 'w') as wheel:
                wheel.writestr('sample/__init__.py', 'VALUE = 1\n')
                wheel.writestr('sample-1.dist-info/METADATA', 'Name: sample\n')

            self.assertEqual(wheel_import_names(artifact), ['sample'])

    def test_invalid_packaging_metadata_is_blocked_at_component_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / 'pyproject.toml').write_text('[project\n')

            with self.assertRaisesRegex(GateError, 'INVALID_PACKAGING_METADATA'):
                wheel_applicability(project)


if __name__ == '__main__':
    unittest.main()
