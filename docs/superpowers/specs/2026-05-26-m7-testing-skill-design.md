# M7 Testing Skill Design

## Goal

Complete M7 by adding the missing test coverage, a minimal CI smoke workflow, and the BookWiki skill documentation required by `design.md` section 20 and `plan.md` M7.

## Scope

- Add or reshape tests for schema snapshots, agent mock execution, scheduler behavior, integrator output, and e2e smoke.
- Add `skills/bookwiki/SKILL.md` plus focused runbook and contract references.
- Add GitHub Actions smoke CI for `pytest -k smoke`.
- Update only the M7 checklist in `plan.md` after verification.

## Architecture

Tests stay close to existing units and reuse the current fake LLM runtime and temporary book fixtures. The skill entrypoint remains short and points to references for detailed stage commands and artifact contracts. CI validates the smoke path without starting the long-running site dev server.

## Testing

Each behavior change gets a failing test first. Verification is complete only after the focused new tests and the full Python test suite pass.
