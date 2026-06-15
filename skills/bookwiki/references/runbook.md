# BookWiki Runbook

## Setup

Use commands from the repository root. For real LLM runs, configure the needed API key before any stage that can miss cache:

```bash
export DEEPSEEK_API_KEY="..."
export MOONSHOT_API_KEY="..."
```

For tests or smoke runs only:

```bash
export BOOKWIKI_TEST_LLM=1
```

## Stage Commands

Initialize a book:

```bash
python scripts/init_book.py books/<id> --source path/to/source.pdf
```

Run the default pipeline through `index`:

```bash
python scripts/run.py books/<id>
```

Resume from a checkpoint:

```bash
python scripts/run.py books/<id> --resume
```

Run one focused stage:

```bash
python scripts/convert.py books/<id>
python scripts/caption.py books/<id>
python scripts/structure.py books/<id>
python scripts/split.py books/<id>
python scripts/generate.py books/<id>
python scripts/check.py books/<id>
python scripts/repair.py books/<id>
python scripts/index.py books/<id>
```

Materialize and preview the site only when explicitly asked:

```bash
python scripts/site.py books/<id>
```

`scripts/site.py` copies `site-template/` into `books/<id>/site/`, copies `content/docs`, preserves `site/.bookwiki/bookwiki.sqlite`, installs packages, and starts `pnpm dev`.

## Structure Review Gate

After `structure`, inspect:

```text
books/<id>/work/structure/proposed-structure.yaml
books/<id>/work/structure/approved-structure.yaml
```

The approved file must contain:

```text
# bookwiki: approved-structure
```

Do not run `split` before the user reviews and approves the structure.

## Resume and Force

Use `--resume` when a run paused or was interrupted:

```bash
python scripts/run.py books/<id> --resume
```

Use `--from <stage> --force` when upstream artifacts are valid but downstream outputs should be regenerated (both flags are required together):

```bash
python scripts/run.py books/<id> --from generate --force
```

Useful cases:

- `--from structure --force`: converted source Markdown is valid; rebuild structure and downstream stages.
- `--from build_skeleton --force`: split chapter sources are valid; rebuild `work/skeleton.json` (the book-wide term contract) and everything after it.
- `--from generate --force`: split chapter sources and skeleton are valid; regenerate agent content and downstream stages.
- `--from check --force`: MDX content is valid; rerun check and downstream routing.

`build_skeleton`, `reconcile_concepts`, `concept_pages`, and `integrate` have no thin stage script; reach them only through `run.py` (e.g. pause at the concept-merge gate with `--pause-after reconcile_concepts`).

Stop entry/exit points with `--to <stage>` or `--pause-after <stage>`, and preview without executing using `--dry-run`.

Do not use `--from split --force` unless `approved-structure.yaml` is already reviewed and marked.

## Check and Repair

Run:

```bash
python scripts/check.py books/<id>
```

Then read:

```text
books/<id>/work/logs/check-report.json
books/<id>/work/logs/check-report.md
```

Decision rules:

- `status: ok`: proceed to `index`.
- `status: needs_repair` with `repair_targets`: run `python scripts/repair.py books/<id> --resume`, then resume the pipeline.
- Warnings only: show the warnings to the user; do not regenerate blindly.
- Unknown source refs or broken links after repair: inspect the related `work/agent_results/*.json` and source manifests before rerunning broad stages.

Notes on the current check/repair contract:

- `generate` and `concept_pages` already self-heal each chapter/concept body inline, so most MDX/citation issues never reach the macro `check`.
- The macro `check` compiles the rendered `.mdx` with the bundled Node validator `tools/mdx-validate` and raises `MDX_PARSE_ERROR` on render-time breakage. It refuses to run if the validator is missing (no Node / no `node_modules`) unless `generation.allowMissingMdxValidator=true`.
- The macro `repair` is deterministic and prefers DROP over fabrication: it removes unverifiable citations, quiz items whose answer is not among the choices, and empty-sided cards. It does not rewrite content. Removals are logged to `work/logs/repair-actions.json`; targets that exhaust `maxRepairRounds` are recorded in `work/logs/repair-exhausted.json`.

## Common Failures

- Missing API key: set `DEEPSEEK_API_KEY` or `MOONSHOT_API_KEY`; do not switch to fake runtime outside tests.
- Structure gate failure: add the exact approval marker after user review.
- Stale content after changing generation settings: use `--from generate --force`.
- `check` aborts on a missing MDX validator: install Node and run the `tools/mdx-validate` install (`node_modules`), or set `generation.allowMissingMdxValidator=true` only if you accept skipping render-time MDX checks.
- `BudgetExceeded`: the run crossed `budget.maxCostCny` (default `70.0`); raise it in `book.config.json` or set `<= 0` for unlimited, then `--resume`.
- SQLite missing: run `python scripts/index.py books/<id>` after content exists and check passes.
- Site has old docs: rerun `python scripts/site.py books/<id>` when preview is requested.
