# Agentic Engineering: project rules

- Approved specification in `specs/` and plan in `plans/` are immutable until renewed human approval.
- Developer loads the **original** `ub-mode` skill and uses Red → Green → Refactor.
- Never weaken tests, quality rules or acceptance criteria just to get green.
- Run deterministic checks automatically, report actual results and limitations.
- No automatic git push, remote writes or deployment.
- Never expose API keys, `.env`, SSH materials or secrets.
- Architect only writes DRAFT documents to `.agentic/drafts/`.
- Reviewer is read-only and must see current verify receipt.
- This workflow does NOT constitute OS sandbox isolation; use a separate Linux user/CI runner for hostile code.
