# BookWiki Runbook

## Setup

Use commands from the repository root. For real LLM runs, configure the needed API key before any stage that can miss cache:

```powershell
$env:DEEPSEEK_API_KEY = "..."
$env:MOONSHOT_API_KEY = "..."
```

For tests or smoke runs only:

```powershell
$env:BOOKWIKI_TEST_LLM = "1"
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

Use `--force-from <stage>` when upstream artifacts are valid but downstream outputs should be regenerated:

```bash
python scripts/run.py books/<id> --force-from generate
```

Useful cases:

- `--force-from structure`: converted source Markdown is valid; rebuild structure and downstream stages.
- `--force-from generate`: split chapter sources are valid; regenerate agent content and downstream stages.
- `--force-from check`: MDX content is valid; rerun check and downstream routing.

Do not use `--force-from split` unless `approved-structure.yaml` is already reviewed and marked.

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

## Common Failures

- Missing API key: set `DEEPSEEK_API_KEY` or `MOONSHOT_API_KEY`; do not switch to fake runtime outside tests.
- Structure gate failure: add the exact approval marker after user review.
- Stale content after changing generation settings: use `--force-from generate`.
- SQLite missing: run `python scripts/index.py books/<id>` after content exists and check passes.
- Site has old docs: rerun `python scripts/site.py books/<id>` when preview is requested.
