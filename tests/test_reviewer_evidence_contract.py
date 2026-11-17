"""Reviewer must not demand dedicated acceptance-check records for every AC."""
import unittest
from pathlib import Path


class ReviewerEvidenceContractTests(unittest.TestCase):
    def test_review_prompt_distinguishes_types_of_evidence(self):
        src = (Path(__file__).resolve().parents[1] / "plugin/index.ts").read_text()
        for required in (
            "Оценивай каждый acceptance criterion",
            "Функциональные AC могут подтверждаться",
            "актуальным VERIFY",
            "НЕ является причиной REJECT",
            "current_pass=true",
            "НЕ обязательное условие для всех AC",
            "Исторические TDD-записи подтверждают",
            "не вводи новых обязательных AC от себя",
        ):
            with self.subTest(required=required):
                self.assertIn(required, src)
        self.assertNotIn(
            "только запись с current_pass=true подтверждает конкретный AC", src
        )


if __name__ == "__main__":
    unittest.main()
