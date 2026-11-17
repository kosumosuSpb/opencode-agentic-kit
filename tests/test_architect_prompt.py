import unittest
from pathlib import Path


class ArchitectPromptContractTests(unittest.TestCase):
    def test_tdd_contract(self):
        text = (
            Path(__file__).resolve().parents[1]
            / "agents/architect.md"
        ).read_text()

        required = [
            "На КАЖДОЙ из трёх стадий ОБЯЗАТЕЛЬНЫ оба запуска",
            "RED: feature и regression завершаются с кодом 1",
            "GREEN: feature и regression завершаются с кодом 0",
            "REFACTOR: feature и regression завершаются с кодом 0",
            "TDD evidence через Runner",
            "Обязательная самопроверка Design",
            "Не объявляй Design успешно завершённым",
        ]

        for phrase in required:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

        self.assertNotIn(
            "Если план требует полный прогон",
            text,
        )


if __name__ == "__main__":
    unittest.main()
