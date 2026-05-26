---
name: bookwiki
description: "Use when working on the BookWiki project: running Python pipeline scripts, converting sources with MinerU, approving structures, generating Fumadocs MDX, checking or repairing output, indexing SQLite, previewing the local site, or maintaining the Next.js/Fumadocs template."
---

# BookWiki

BookWiki turns one book's source materials into an Obsidian-style vault, a SQLite index, and a local learning site. Prefer the scripted pipeline over ad hoc commands.

## Standard Flow

1. Run stages in this order: `convert -> structure -> approve -> split -> generate -> check/repair -> index -> site`.
2. Use `python scripts/run.py books/<id>` for the full pipeline through `index`.
3. Use `python scripts/run.py books/<id> --resume` after the structure review gate or after an interrupted run.
4. Use thin stage scripts for focused work: `scripts/convert.py`, `scripts/structure.py`, `scripts/split.py`, `scripts/generate.py`, `scripts/check.py`, `scripts/repair.py`, `scripts/index.py`.
5. Do not start `scripts/site.py` unless the user asks for site preview or verification.

## Hard Gate

The `structure` stage pauses before `split`. Before running `split`, the user must review `work/structure/proposed-structure.yaml`, edit `work/structure/approved-structure.yaml`, and include a line exactly:

```text
# bookwiki: approved-structure
```

If that marker is missing, stop and ask for review instead of bypassing the gate.

## LLM Runtime

Agents return Pydantic models. Cache misses call the configured real LLM through `bookwiki.scheduler.llm`.

- Configure `DEEPSEEK_API_KEY` for `deepseek-*` models.
- Configure `MOONSHOT_API_KEY` for `kimi-*` models.
- Keep keys in the environment or repo root `.env`; existing environment variables take precedence.
- Missing keys should fail loudly. Do not silently fall back to stub content.
- Tests may use `BOOKWIKI_TEST_LLM=1` for the explicit fake runtime.

## Failure Triage

Check these first:

- `work/logs/run-manifest.json`: pipeline status, next node, cache hits, and outputs.
- `work/logs/check-report.json`: blocking issues and `repair_targets`.
- `work/logs/check-report.md`: human-readable check report.
- `work/logs/chapter-split-report.md`: split coverage and assignment notes.
- `work/agent_results/*.json`: raw Pydantic agent outputs.

If `check-report.json` has error or critical `repair_targets`, run `python scripts/repair.py books/<id> --resume`, then resume through check/index. If it has only warnings, decide with the user whether to accept or revise.

## References

- Read `references/runbook.md` for stage commands, `--force-from`, resume, site materialization, and common failures.
- Read `references/contracts.md` for the key artifact contracts: approved structure, reconciled concepts, check report, run manifest, and SQLite schema.
