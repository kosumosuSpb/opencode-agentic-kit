from __future__ import annotations

import re
import unittest
from fnmatch import fnmatchcase
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def rules(name: str) -> list[tuple[str, str, str]]:
    text = (ROOT / 'agents' / name).read_text()
    return re.findall(r'- action: ([^\n]+)\n\s+resource: "([^"]+)"\n\s+effect: ([^\n]+)', text)


def effect(name: str, action: str, resource: str) -> str:
    return effect_from_rules(rules(name), action, resource)


def effect_from_rules(ordered: list[tuple[str, str, str]], action: str, resource: str) -> str:
    found = 'ask'
    for rule_action, pattern, rule_effect in ordered:
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
            self.assertEqual(effect(agent, 'shell', 'git diff --stat'), 'allow')
            self.assertEqual(effect(agent, 'shell', 'git log --oneline -5'), 'allow')
            self.assertEqual(effect(agent, 'shell', 'git diff --no-ext-diff --stat'), 'allow')
            self.assertEqual(effect(agent, 'shell', 'ls -la'), 'ask')
            self.assertEqual(effect(agent, 'shell', 'find . -maxdepth 3 -type f'), 'ask')
            self.assertEqual(effect(agent, 'shell', 'python3 --version'), 'ask')
            self.assertEqual(effect(agent, 'shell', 'python3 -m pytest --version'), 'ask')
            self.assertEqual(effect(agent, 'shell', 'rm -rf target'), 'ask')
            self.assertEqual(effect(agent, 'shell', 'git status --short > status.txt'), 'ask')
            self.assertEqual(effect(agent, 'shell', 'git diff --no-ext-diff --stat | wc -l'), 'ask')
            self.assertEqual(
                request_effect(agent, 'shell', ['git status --short', 'rm -rf target']), 'ask'
            )

    def test_only_reviewer_allows_read_only_diff_pathspecs(self):
        for resource in (
            'git diff -- src/app.py',
            'git diff --no-ext-diff -- src/app.py',
            'git diff --cached -- tests/test_app.py',
        ):
            self.assertEqual(effect('reviewer.md', 'shell', resource), 'allow')
            self.assertEqual(effect('architect.md', 'shell', resource), 'ask')
            self.assertEqual(effect('developer.md', 'shell', resource), 'ask')

    def test_git_commit_asks_and_remote_writes_are_denied(self):
        for agent in ('architect.md', 'developer.md', 'reviewer.md'):
            self.assertEqual(effect(agent, 'shell', 'git commit -am change'), 'ask')
            self.assertEqual(effect(agent, 'shell', 'git push origin main'), 'deny')
            self.assertEqual(effect(agent, 'shell', 'uv publish dist/pkg.whl'), 'deny')
            self.assertEqual(effect(agent, 'shell', 'twine upload dist/*'), 'deny')

    def test_role_edit_boundaries(self):
        self.assertEqual(effect('architect.md', 'edit', '.agentic/drafts/spec.md'), 'allow')
        self.assertEqual(
            effect('architect.md', 'edit', '.agentic/tasks/task-a/drafts/spec.md'), 'deny'
        )
        self.assertEqual(effect('architect.md', 'edit', 'src/app.py'), 'deny')
        self.assertEqual(effect('reviewer.md', 'edit', 'src/app.py'), 'deny')
        self.assertEqual(effect('developer.md', 'edit', 'src/app.py'), 'allow')
        self.assertEqual(effect('developer.md', 'edit', 'arithmetic.py'), 'allow')
        self.assertEqual(effect('developer.md', 'edit', 'tests/test_arithmetic.py'), 'allow')
        self.assertEqual(effect('developer.md', 'edit', 'specs/approved.md'), 'deny')
        self.assertEqual(effect('developer.md', 'edit', 'plans/approved.md'), 'deny')
        self.assertEqual(effect('developer.md', 'edit', 'specs/tasks/task-a/approved.md'), 'deny')
        self.assertEqual(effect('developer.md', 'edit', 'plans/tasks/task-a/approved.md'), 'deny')
        self.assertEqual(effect('developer.md', 'edit', '.git/config'), 'deny')

    def test_reviewer_guidance_uses_builtins_and_runner_evidence(self):
        text = (ROOT / 'agents/reviewer.md').read_text()
        self.assertIn('`read`, `glob`, `grep`', text)
        self.assertIn('Не запускай через shell `ls`, `find`, `cat`', text)
        self.assertIn('review-context verify/full', text)
        self.assertIn('временный каталог', text)
        self.assertIn('не является доказуемо read-only', text)
        self.assertIn('review-submit', text)
        self.assertIn('`exception`', text)
        self.assertIn('/accept <review-id>', text)
        self.assertIn('carried findings', text)
        self.assertIn('FULL fallback review', text)

    def test_architect_active_task_scope_overrides_inherited_legacy_deny_only(self):
        inherited = [
            ('edit', '*', 'deny'),
            ('edit', '.agentic/drafts/*', 'allow'),
        ]
        active_scope = [
            ('edit', '.agentic/tasks/*/drafts/**', 'deny'),
            ('edit', '.agentic/drafts/*', 'deny'),
            ('edit', '.agentic/tasks/task-a/drafts/spec.md', 'allow'),
            ('edit', '.agentic/tasks/task-a/drafts/plan.md', 'allow'),
        ]
        effective = [*rules('architect.md'), *inherited, *active_scope]
        self.assertEqual(
            effect_from_rules(effective, 'edit', '.agentic/tasks/task-a/drafts/spec.md'), 'allow'
        )
        self.assertEqual(
            effect_from_rules(effective, 'edit', '.agentic/tasks/task-a/drafts/plan.md'), 'allow'
        )
        for denied in (
            '.agentic/tasks/task-a/drafts/notes.md',
            '.agentic/tasks/task-b/drafts/spec.md',
            'src/app.py',
            'specs/tasks/task-a/approved.md',
            'plans/tasks/task-a/approved.md',
            '.git/config',
        ):
            self.assertEqual(effect_from_rules(effective, 'edit', denied), 'deny', denied)

    def test_architect_documents_only_supported_tdd_protocol(self):
        text = (ROOT / 'agents/architect.md').read_text()
        self.assertIn('feature', text)
        self.assertIn('regression', text)
        self.assertIn('RED', text)
        self.assertIn('GREEN', text)
        self.assertIn('REFACTOR', text)
        self.assertIn('код 2', text.lower())
        self.assertIn('единственной строке метаданных', text)
        self.assertNotIn('Этот документ является черновиком', text)


if __name__ == '__main__':
    unittest.main()
