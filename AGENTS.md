# Agentic Engineering: project rules

- Approved specification in `specs/` and plan in `plans/` are immutable until renewed human approval.
- Developer loads the **original** `ub-mode` skill and uses Red → Green → Refactor.
- Never weaken tests, quality rules or acceptance criteria just to get green.
- Run deterministic checks automatically, report actual results and limitations.
- No automatic git push, remote writes or deployment.
- Never expose API keys, `.env`, SSH materials or secrets.
- Architect only writes DRAFT documents to the active task directory at
  `.agentic/tasks/<id>/drafts/`. The old `.agentic/drafts/` layout is supported
  only for legacy tasks.
- Reviewer is read-only and must see current verify receipt.
- This workflow does NOT constitute OS sandbox isolation; use a separate Linux user/CI runner for hostile code.

## Project checks

- Use the exact Runner command supplied by Agentic Kit. For direct repository
  runs, use `python3 runner/agentic_gate.py --project "$PWD" gate fast`,
  `python3 runner/agentic_gate.py --project "$PWD" gate verify`, or
  `python3 runner/agentic_gate.py --project "$PWD" gate full`. Use targeted
  tests while developing, FAST after a coherent unit, VERIFY before review, and
  FULL only for release/finalization.
- Run the Python regression suite with
  `python3 -m unittest discover -s tests -v`.
- Run the TypeScript/JavaScript plugin suite separately with
  `nvm exec 24 node tests/test_plugin.cjs`; the Python Runner does not replace
  this language-specific check.
- Runner-managed Python quality checks are Ruff check/format, Mypy, Pytest,
  pytest-cov, Crapper (CRAP ≤ 10), Dryer (duplicate score < 0.8), and, for FULL,
  Mutator. Report a missing tool as `NOT RUN`; do not install or add dependencies
  merely to make a gate available.

## Installer invariants

- Preserve the transactional install/upgrade/rollback model and idempotent crash
  recovery. Changes in those paths require explicit interrupted-operation and
  recovery tests.
- Exercise installer filesystem operations only with isolated temporary
  `HOME`/XDG directories. Never use or modify the real global installation in
  installer tests.
