import unittest
from pathlib import Path


class ImplementPromptTests(unittest.TestCase):
    def test_approved_plan_controls_tdd_commands(self):
        source = (
            Path(__file__).resolve().parents[1] / "plugin/index.ts"
        ).read_text()

        for expected in (
            "Команды TDD feature и regression после -- бери из APPROVED PLAN",
            "python.test_command относится к Quality Gates",
            "НЕ переопределяет",
            "<feature-test-command-from-approved-plan>",
            "<regression-test-command-from-approved-plan>",
            "python -m unittest",
            "python -m pytest",
            "const gateTest = shellCommand",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, source)

        self.assertNotIn("-- ${test} <test-path>", source)
        self.assertNotIn("-- ${test} tests", source)

    def test_prompt_distinguishes_new_tdd_from_continuation(self):
        source = (
            Path(__file__).resolve().parents[1] / "plugin/index.ts"
        ).read_text()

        for expected in (
            "Первичная реализация",
            "Продолжение после завершённого TDD",
            "не создавай новый RED",
            "Изменённый approval",
            "workflow_state",
            "has_completed_cycle не означает",
            "all_recorded_cycles_complete",
            "baseline_current",
            "каждый цикл с APPROVED PLAN",
            "нового поведения",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, source)


if __name__ == "__main__":
    unittest.main()
