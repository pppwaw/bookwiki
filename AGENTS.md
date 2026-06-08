# BookWiki Project Instructions

BookWiki turns one book's source materials into an Obsidian-style vault, a SQLite index, and a local learning site.

Use `scripts/run.py <book_dir>` for the full pipeline. Use the thin stage scripts for focused work:
`convert`, `caption`, `structure`, `split`, `generate`, `check`, `repair`, and `index`. The full graph
runs `convert → caption → structure → split → generate → reconcile_concepts → concept_pages → integrate
→ check → repair → index`. Control entry/exit with `--from <stage> --force`, `--to <stage>`,
`--pause-after <stage>`, `--resume`, and `--dry-run` (there is no `--force-from`).

The default runnable pipeline scope is the full graph through `index`; the pipeline is feature-complete,
with only optional follow-ups left. Do not start the long-running site dev server
(`scripts/site.py`) by default unless the user asks for site preview or verification.

The `convert` stage parses PDF/PPTX through MinerU (failing loudly instead of degrading to local
extraction) and may run an LLM layout repair; `caption` then sends the extracted figures to the vision
model to fill in image captions before structuring.

The `structure` stage is a hard review gate. Before running `split`, the user must review
`work/structure/proposed-structure.yaml`, edit `work/structure/approved-structure.yaml`, and mark
it with a line exactly `# bookwiki: approved-structure`.

Keep agent outputs as Pydantic models. Agents do not write final Markdown; scheduler nodes write intermediate JSON and the integrator renders the vault.

The `generate` stage is agentic and runs per chapter: `SectionPlannerAgent` splits the chapter into
teaching units, `SectionAgent` writes each section's prose (section-level validate/repair with a
`maxSectionRepairRounds` fallback that logs a warning and keeps the imperfect section), `QuizCardAgent`
produces the quiz and recall cards from the assembled body, and `SummaryAgent` writes the summary. When
a section needs a figure the source PDF lacks, `SectionAgent` declares a `figure_request` and
`SupplementImageAgent` fills it by writing matplotlib code through LiteLLM function-calling; generated
figures land under `work/assets/generated/` and are merged into the chapter figure index so the
integrator keeps them.

`run_plot` (the figure tool) executes LLM-written matplotlib code via **host subprocess** behind three
guardrails (AST import/call blacklist, chdir to an isolated tempdir with a scrubbed environment, and a
wall-clock timeout plus POSIX rlimits), with deterministic output (Agg backend, seeded RNG, locked
font) keyed by `sha256(code)`. This is deliberate host execution, not a hardened sandbox, because the
**threat model is a single-user local tool**: the LLM is the user's own paid (semi-trusted) DeepSeek/
Kimi model and output is rendered locally. If BookWiki is ever exposed as a multi-tenant web service,
`run_plot` MUST be upgraded to a real sandbox (Docker hardening / gVisor / E2B) and re-reviewed against
OWASP ASI05.

Agent cache misses call the configured real LLM through `bookwiki.scheduler.llm`. Configure
`DEEPSEEK_API_KEY` for `deepseek-*` models and `MOONSHOT_API_KEY` for `kimi-*` models (including the
`kimi-k2.6` vision captioner) in the process environment or in the repo root `.env`; existing
environment variables take precedence. The site's `/api/chat` route uses `BOOKWIKI_CHAT_API_KEY`
(OpenRouter). Missing keys should fail loudly rather than falling back to stub content. Tests may opt
into the explicit `BOOKWIKI_TEST_LLM=1` fake runtime.

