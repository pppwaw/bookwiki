# BookWiki Project Instructions

BookWiki turns one book's source materials into an Obsidian-style vault, a SQLite index, and a local learning site.

Use `scripts/run.py <book_dir>` for the full stub pipeline. Use the thin stage scripts for focused work:
`convert`, `structure`, `split`, `generate`, `check`, `repair`, and `index`.

Keep agent outputs as Pydantic models. Agents do not write final Markdown; scheduler nodes write intermediate JSON and the integrator renders the vault.

For M0/M1, all agent implementations are deterministic stubs and must not call external LLM APIs.
