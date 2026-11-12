from __future__ import annotations

from fnmatch import fnmatchcase
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


def rules(name: str) -> list[tuple[str, str, str]]:
    text = (ROOT / 'agents' / name).read_text()
    return re.findall(
        r'- action: ([^\n]+)\n\s+resource: "([^"]+)"\n\s+effect: ([^\n]+)', text
    )


def effect(name: str, action: str, resource: str) -> str:
    found = 'ask'
    for rule_action, pattern, rule_effect in rules(name):
        if fnmatchcase(action, rule_action) and fnmatchcase(resource, pattern):
            found = rule_effect
    return found


def request_effect(name: str, action: str, resources: list[str]) -> str:
    effects = [effect(name, action, resource) for resource in resources]
    if 'deny' in effects:
        return 'deny'
    if 'ask' in effects:
        return 'ask'
    return 'allow'


class AgentPermissionTests(unittest.TestCase):
    def test_safe_discovery_and_git_inspection(self):
        for agent in ('architect.md', 'developer.md', 'reviewer.md'):
            self.assertEqual(effect(agent, 'read', 'src/app.py'), 'allow')
            self.assertEqual(effect(agent, 'glob', '**/*.py'), 'allow')
            self.assertEqual(effect(agent, 'grep', 'needle'), 'allow')
            self.assertEqual(effect(agent, 'shell', 'git status --short'), 'allow')
            self.assertEqual(effect(agent, 'shell', 'git diff --no-ext-diff --stat'), 'allow')
            self.assertEqual(effect(agent, 'shell', 'rm -rf target'), 'ask')
            self.assertEqual(effect(agent, 'shell', 'git status --short > status.txt'), 'ask')
            self.assertEqual(effect(agent, 'shell', 'git diff --no-ext-diff --stat | wc -l'), 'ask')
            self.assertEqual(request_effect(agent, 'shell',
                                             ['git status --short', 'rm -rf target']), 'ask')

    def test_git_commit_asks_and_remote_writes_are_denied(self):
        for agent in ('architect.md', 'developer.md', 'reviewer.md'):
            self.assertEqual(effect(agent, 'shell', 'git commit -am change'), 'ask')
            self.assertEqual(effect(agent, 'shell', 'git push origin main'), 'deny')
            self.assertEqual(effect(agent, 'shell', 'uv publish dist/pkg.whl'), 'deny')
            self.assertEqual(effect(agent, 'shell', 'twine upload dist/*'), 'deny')

    def test_role_edit_boundaries(self):
        self.assertEqual(effect('architect.md', 'edit', '.agentic/drafts/spec.md'), 'allow')
        self.assertEqual(effect('architect.md', 'edit', 'src/app.py'), 'deny')
        self.assertEqual(effect('reviewer.md', 'edit', 'src/app.py'), 'deny')
        self.assertEqual(effect('developer.md', 'edit', 'src/app.py'), 'ask')
        self.assertEqual(effect('developer.md', 'edit', 'specs/approved.md'), 'deny')
        self.assertEqual(effect('developer.md', 'edit', 'plans/approved.md'), 'deny')
        self.assertEqual(effect('developer.md', 'edit', '.git/config'), 'deny')


if __name__ == '__main__':
    unittest.main()
