---
name: bookwiki
description: "Use when working on the BookWiki project: running Python pipeline scripts, converting sources with MinerU, approving structures, generating Fumadocs MDX, checking or repairing output, indexing SQLite, previewing the local site, or maintaining the Next.js/Fumadocs template."
---

# BookWiki

BookWiki turns one book's source materials into an Obsidian-style vault, a SQLite index, and a local learning site. Prefer the scripted pipeline over ad hoc commands.

## Standard Flow

1. The frozen pipeline is the 12-node `NODE_ORDER` (`bookwiki/scheduler/resume.py`):
   `convert -> caption -> structure -> split -> build_skeleton -> generate -> reconcile_concepts -> concept_pages -> integrate -> check -> repair -> index`.
   The user-facing sequence is `convert -> caption -> structure -> approve -> split -> build_skeleton -> generate -> check/repair -> index -> site`; `reconcile_concepts -> concept_pages -> integrate` run inside the graph between `generate` and `check`.
2. Use `python scripts/run.py books/<id>` for the full pipeline through `index`.
3. Use `python scripts/run.py books/<id> --resume` after the structure review gate or after an interrupted run.
4. Use thin stage scripts for focused work: `scripts/convert.py`, `scripts/caption.py`, `scripts/structure.py`, `scripts/split.py`, `scripts/generate.py`, `scripts/check.py`, `scripts/repair.py`, `scripts/index.py`. There is no thin script for `build_skeleton`, `reconcile_concepts`, `concept_pages`, or `integrate`; reach them through `run.py` (e.g. `--pause-after reconcile_concepts`).
5. Do not start `scripts/site.py` unless the user asks for site preview or verification.

## Generation Stages (what each node does)

- `convert`: parse PDF/PPTX through MinerU (fail loud, no local fallback), optional LLM layout repair, write `work/sources_md/` + `work/source_refs/<source_id>.json` manifests.
- `caption`: send extracted figures to the `kimi-k2.6` vision model and fill image captions into the manifests and markdown.
- `structure`: `StructureAgent` emits `work/structure/proposed-structure.yaml` (code generates the YAML from structured output). HARD GATE before `split` (see below). Two-level chapter grouping (Chapter N -> N.M) is supported.
- `split`: `ChapterSplitAgent` aligns source fragments by topic into `work/chapter_sources/<id>/source.md` + `_alignment.json`.
- `build_skeleton`: `SkeletonAgent` runs once over every chapter source to produce the book-wide read-only contract `work/skeleton.json` (canonical glossary with each concept's first-owning chapter, an `alias_map` of every variant -> canonical, and one-line `chapter_briefs`). `generate` injects each chapter's slice so chapters share terminology and can write neighbour transitions.
- `generate`: per-chapter section pipeline (`bookwiki/generate/sections.py`). `SectionPlannerAgent` splits the chapter into teaching units; each section (parallel, bounded by `maxSectionConcurrency`) runs `SectionAgent` (MDX-direct flat frontmatter) + `KnowledgeQuizAgent` (per-section quiz, JSON/flash) + section validate/repair (`RepairSectionAgent`, up to `maxSectionRepairRounds`, fallback keeps the imperfect section with a warning); a missing figure becomes a `figure_request` filled by `SupplementImageAgent` via matplotlib `run_plot`. The assembled chapter runs inline MDX/quality self-heal (`ChapterMdxEditRepairAgent` / `ChapterContentRewriteAgent`), then `ApplicationQuizAgent`, `CardAgent`, `SummaryAgent`. Writes `work/agent_results/*.json` only (no MDX).
- `reconcile_concepts`: `ConceptReconcileAgent` converges per-chapter concept candidates into `work/concepts/reconciled.json` + `alias_map`.
- `concept_pages`: `ConceptAgent` writes one page per unique concept, with inline MDX/quality self-heal.
- `integrate`: render the Fumadocs vault MDX, rewrite `[[alias]]` to canonical, resolve cross-chapter mentions into `<PreviewLink>`, and audit residual term drift / unresolved cross-references (`bookwiki/integrator/stitching.py`).
- `check`: cross-cutting checks PLUS rendered-MDX compilation via the bundled Node validator `tools/mdx-validate` (`@mdx-js/mdx` + remark-math); raises `MDX_PARSE_ERROR` on render-time breakage. It refuses to run if the validator is unavailable unless `generation.allowMissingMdxValidator=true`.
- `repair`: deterministic DROP-over-fabrication (`bookwiki/pipeline/nodes.py` `_apply_repair`). Unverifiable citations, quiz items whose answer is not among the choices, and empty-sided cards are removed (not re-attributed / rewritten), with an audit trail in `work/logs/repair-actions.json`; targets that exhaust `maxRepairRounds` go to `work/logs/repair-exhausted.json`.
- `index`: `build_sqlite_index` rebuilds `site/.bookwiki/bookwiki.sqlite` from the rendered content.

Validation is pushed down: `generate` / `concept_pages` self-heal raw pre-render bodies inline (bottoming out as warnings). Semantic quality stays default-off (`generation.qualityCheck=false`); when off, no quality LLM call is made.

## Hard Gate

The `structure` stage pauses before `split`. Before running `split`, the user must review `work/structure/proposed-structure.yaml`, edit `work/structure/approved-structure.yaml`, and include a line exactly:

```text
# bookwiki: approved-structure
```

If that marker is missing, stop and ask for review instead of bypassing the gate.

## LLM Runtime

Agents return Pydantic models. Cache misses call the configured real LLM through `bookwiki.scheduler.llm`. `lg_runner` injects one shared `LiteLLMRuntime` onto `cfg.llm_runtime` for the whole run, so every agent reuses one LiteLLM `Router` (tpm/rpm throttling + usage/cost accounting are per-Router).

- Configure `DEEPSEEK_API_KEY` for `deepseek-*` models.
- Configure `MOONSHOT_API_KEY` for `kimi-*` models, including the `kimi-k2.6` vision captioner used by the `caption` stage.
- Optional API Base URL overrides: `DEEPSEEK_API_BASE_URL` / `MOONSHOT_API_BASE_URL` (short aliases `DEEPSEEK_API_BASE` / `MOONSHOT_API_BASE`). Moonshot defaults to `https://api.moonshot.cn/v1`; DeepSeek uses LiteLLM's provider default unless overridden.
- The site's `/api/chat` route uses `BOOKWIKI_CHAT_API_KEY` (OpenRouter).
- Keep keys in the environment or repo root `.env`; existing environment variables take precedence.
- Missing keys should fail loudly. Do not silently fall back to stub content.
- Tests may use `BOOKWIKI_TEST_LLM=1` for the explicit fake runtime.
- Budget is enforced: `budget.maxCostCny` (default `70.0`; `<= 0` means unlimited) raises `BudgetExceeded` once the running total crosses it. Prices are registered per Router deployment in CNY. Legacy `maxCostUsd` configs are migrated to `maxCostCny`.

## Failure Triage

Check these first:

- `work/logs/run-manifest.json`: pipeline status, next node, cache hits, outputs, and `llm_usage` actual token / CNY spend (`llm_usage.stages` has per-stage deltas).
- `work/logs/check-report.json`: blocking issues and `repair_targets`.
- `work/logs/check-report.md`: human-readable check report.
- `work/logs/chapter-split-report.md`: split coverage and assignment notes.
- `work/logs/repair-actions.json`: what the `repair` node dropped (citations / quiz items / cards).
- `work/logs/repair-exhausted.json`: targets that exhausted `maxRepairRounds` instead of being dropped.
- `work/skeleton.json` and `work/concepts/reconciled.json` (+ `work/concepts/alias_map.json`): the book-wide term contracts.
- `work/agent_results/*.json`: raw Pydantic agent outputs.

If `check-report.json` has error or critical `repair_targets`, run `python scripts/repair.py books/<id> --resume`, then resume through check/index. If it has only warnings, decide with the user whether to accept or revise. Note that `generate`/`concept_pages` already self-heal inline, so many issues never reach the macro `check`; the macro `repair` only drops unsalvageable items (it does not rewrite content).

## References

- Read `references/runbook.md` for stage commands, `--from <stage> --force`, resume, site materialization, and common failures.
- Read `references/contracts.md` for the key artifact contracts: approved structure, reconciled concepts, check report, run manifest, and SQLite schema.
