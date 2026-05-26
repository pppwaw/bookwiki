# M7 Testing Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete M7 with focused tests, smoke CI, and BookWiki skill documentation.

**Architecture:** Keep tests at existing module boundaries and avoid changing pipeline behavior unless a test exposes a real M7 gap. Skill docs provide a compact AI entrypoint and defer detailed contracts to reference files. CI runs the smoke subset with the explicit fake LLM runtime.

**Tech Stack:** Python 3.12+, pytest, Pydantic v2, local BookWiki scripts, GitHub Actions, Markdown skills.

---

### Task 1: Schema Snapshot Tests

**Files:**
- Modify: `tests/test_schemas.py`

- [ ] **Step 1: Write failing tests**

Add a parametrized test that validates representative JSON payloads for every Pydantic model and asserts `model_dump(mode="json")` matches the expected snapshot.

- [ ] **Step 2: Run the focused test**

Run: `uv run pytest tests/test_schemas.py -v`
Expected: FAIL until all model snapshots are covered.

- [ ] **Step 3: Implement the snapshots**

Use inline fixture dictionaries so schema changes require deliberate test changes.

- [ ] **Step 4: Verify the focused test**

Run: `uv run pytest tests/test_schemas.py -v`
Expected: PASS.

### Task 2: Agent Mock Tests

**Files:**
- Create: `tests/test_agents.py`

- [ ] **Step 1: Write failing tests**

Create a test runtime that calls `litellm.completion(..., mock_response=...)` and validates every LLM-backed agent through the normal `run()` method. Cover deterministic agents that do not call the runtime in the same file.

- [ ] **Step 2: Run the focused test**

Run: `uv run pytest tests/test_agents.py -v`
Expected: FAIL before the test runtime and fixtures are complete.

- [ ] **Step 3: Implement test fixtures**

Provide minimal inputs and mock JSON payloads for source summary, structure, split, chapter, summary, quiz, card, concept reconcile, concept page, review, source layout repair, and deterministic concept extraction.

- [ ] **Step 4: Verify the focused test**

Run: `uv run pytest tests/test_agents.py -v`
Expected: PASS.

### Task 3: Scheduler Tests

**Files:**
- Create: `tests/test_scheduler.py`

- [ ] **Step 1: Write failing tests**

Cover graph topology, fan-out stage behavior in generate/reconcile/concept pages, `run_with_cache` cache hit/miss, and `resume_or_start` resume branches.

- [ ] **Step 2: Run the focused test**

Run: `uv run pytest tests/test_scheduler.py -v`
Expected: FAIL until scheduler assertions and fixtures are complete.

- [ ] **Step 3: Implement scheduler fixtures**

Use `BookConfig` with `tmp_path`, monkeypatch node functions where resume behavior is under test, and reuse `RecordingRuntime` where agent fan-out needs deterministic output.

- [ ] **Step 4: Verify the focused test**

Run: `uv run pytest tests/test_scheduler.py -v`
Expected: PASS.

### Task 4: Integrator Snapshot Test

**Files:**
- Create: `tests/test_integrator.py`

- [ ] **Step 1: Write failing test**

Build fixed agent-result JSON files, run `integrate_node()`, and compare generated MDX content to exact expected strings.

- [ ] **Step 2: Run the focused test**

Run: `uv run pytest tests/test_integrator.py -v`
Expected: FAIL until fixtures and expected MDX are complete.

- [ ] **Step 3: Implement fixed fixtures**

Create chapter, summary, quiz, card, concept reconcile, and concept page JSON under `tmp_path`.

- [ ] **Step 4: Verify the focused test**

Run: `uv run pytest tests/test_integrator.py -v`
Expected: PASS.

### Task 5: E2E Smoke and CI

**Files:**
- Create: `tests/test_e2e_smoke.py`
- Create: `.github/workflows/ci.yml`

- [ ] **Step 1: Write failing smoke test**

Mark a mini-book fake LLM flow with `@pytest.mark.smoke` and assert it reaches SQLite plus site materialization without starting the long-running dev server.

- [ ] **Step 2: Run the smoke subset**

Run: `uv run pytest -k smoke -v`
Expected: FAIL before the smoke marker/file is complete.

- [ ] **Step 3: Add CI workflow**

Install the project with dev dependencies and run `BOOKWIKI_TEST_LLM=1 pytest -k smoke`.

- [ ] **Step 4: Verify smoke locally**

Run: `uv run pytest -k smoke -v`
Expected: PASS.

### Task 6: BookWiki Skill

**Files:**
- Create: `skills/bookwiki/SKILL.md`
- Create: `skills/bookwiki/references/runbook.md`
- Create: `skills/bookwiki/references/contracts.md`

- [ ] **Step 1: Write skill docs**

Create the required YAML frontmatter, standard stage order, failure files, stage commands, force/resume guidance, and artifact contracts.

- [ ] **Step 2: Self-check skill instructions**

Read only `skills/bookwiki/SKILL.md` and confirm it points an AI agent to the right references and gates.

### Task 7: Verification and Plan Update

**Files:**
- Modify: `plan.md`

- [ ] **Step 1: Run full verification**

Run: `uv run pytest`
Expected: PASS.

- [ ] **Step 2: Update M7 checklist**

Mark the completed M7 items in `plan.md` as `[x]`.

- [ ] **Step 3: Check git status**

Run: `git -c safe.directory=C:/Users/pppwa/PycharmProjects/bookwiki status --short`
Expected: only intentional M7 files changed.
