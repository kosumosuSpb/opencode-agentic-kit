import unittest
from pathlib import Path


class TUICommandRegistrationTests(unittest.TestCase):
    def test_client_does_not_duplicate_server_slash_commands(self):
        root = Path(__file__).resolve().parents[1]
        tui = (root / "plugin/tui.ts").read_text()
        server = (root / "plugin/index.ts").read_text()

        self.assertIn("palette: true", tui)
        self.assertIn("ctx.client.session.command({", tui)
        self.assertNotRegex(tui, r"(?m)^\\s*slash\\s*:")

        for name in (
            "design", "task", "approve", "implement",
            "review", "finalize", "accept",
        ):
            with self.subTest(name=name):
                self.assertIn(f'name: "{name}"', server)


if __name__ == "__main__":
    unittest.main()
