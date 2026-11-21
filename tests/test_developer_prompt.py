import unittest
from pathlib import Path


class DeveloperPromptContractTests(unittest.TestCase):
    def test_tdd_command_contract(self):
        text = (
            Path(__file__).resolve().parents[1]
            / "agents/developer.md"
        ).read_text()

        required = [
            "Утверждённый plan определяет тестовые команды",
            "Поле `python.test_command`",
            "НЕ заменяет команды TDD",
            "python -m unittest",
            "python -m pytest",
            "Не подменяй unittest на pytest",
            "Перед первым RED сверь",
            "Не меняй утверждённый plan",
            "компактный `workflow_state`",
            "после завершённого TDD",
            "не создавай новый RED",
            "после `/approve replace`",
            "`has_completed_cycle` не означает",
            "`all_recorded_cycles_complete`",
            "`baseline_current`",
            "каждый цикл с approved plan",
        ]

        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

        self.assertNotIn(
            "точные команды для текущего проекта приходят в `/implement`",
            text,
        )


if __name__ == "__main__":
    unittest.main()
