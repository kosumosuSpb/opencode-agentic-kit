import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

RUNNER = Path(__file__).resolve().parents[1] / 'runner' / 'agentic_gate.py'


class TaskStatusEmptyTest(unittest.TestCase):
    def test_status_without_active_task(self):
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(['git', '-C', directory, 'init', '-q'], check=True)

            result = subprocess.run(
                [sys.executable, str(RUNNER), '--project', directory, 'task-status'],
                capture_output=True,
                text=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout),
                {'version': 2, 'active_task': None, 'tasks': {}},
            )


if __name__ == '__main__':
    unittest.main()
