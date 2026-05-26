# BookWiki Project Instructions

BookWiki turns one book's source materials into an Obsidian-style vault, a SQLite index, and a local learning site.

Use `scripts/run.py <book_dir>` for the full pipeline. Use the thin stage scripts for focused work:
`convert`, `structure`, `split`, `generate`, `check`, `repair`, and `index`.

Do not run pipeline stages beyond the currently completed milestone in `plan.md` unless the user
explicitly asks for that later milestone. For the current plan state, M6b is complete, so the
default runnable pipeline scope may continue through `index`. Do not start the long-running site
dev server (`scripts/site.py`) by default unless the user asks for site preview or verification.

The `structure` stage is a hard review gate. Before running `split`, the user must review
`work/structure/proposed-structure.yaml`, edit `work/structure/approved-structure.yaml`, and mark
it with a line exactly `# bookwiki: approved-structure`.

Keep agent outputs as Pydantic models. Agents do not write final Markdown; scheduler nodes write intermediate JSON and the integrator renders the vault.

Agent cache misses call the configured real LLM through `bookwiki.scheduler.llm`. Configure
`DEEPSEEK_API_KEY` for `deepseek-*` models and `MOONSHOT_API_KEY` for `kimi-*` models in the
process environment or in the repo root `.env`; existing environment variables take precedence.
Missing keys should fail loudly rather than falling back to stub content. Tests may opt into the
explicit `BOOKWIKI_TEST_LLM=1` fake runtime.

