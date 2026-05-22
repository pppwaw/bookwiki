# BookWiki Project Instructions

BookWiki turns one book's source materials into an Obsidian-style vault, a SQLite index, and a local learning site.

Use `scripts/run.py <book_dir>` for the full pipeline. Use the thin stage scripts for focused work:
`convert`, `structure`, `split`, `generate`, `check`, `repair`, and `index`.

Keep agent outputs as Pydantic models. Agents do not write final Markdown; scheduler nodes write intermediate JSON and the integrator renders the vault.

Agent cache misses call the configured real LLM through `bookwiki.scheduler.llm`. Configure
`DEEPSEEK_API_KEY` for `deepseek-*` models and `MOONSHOT_API_KEY` for `kimi-*` models. Missing
keys should fail loudly rather than falling back to stub content. Tests may opt into the explicit
`BOOKWIKI_TEST_LLM=1` fake runtime.

For M2 conversion, PDF parsing requires the local MinerU API at `MINERU_API_URL`; tests that do
not start MinerU should use TXT/PPTX inputs instead of PDF fixtures.
