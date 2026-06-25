# BookWiki Project Instructions

BookWiki turns one book's source materials into an Obsidian-style vault, a SQLite index, and a local learning site.

Use `scripts/run.py <book_dir>` for the full pipeline. Use the thin stage scripts for focused work:
`convert`, `caption`, `structure`, `split`, `generate`, `check`, `repair`, and `index`. The full graph
runs `convert → caption → structure → split → build_skeleton → generate → reconcile_concepts →
concept_pages → integrate → check → repair → index`. Control entry/exit with `--from <stage> --force`,
`--to <stage>`, `--pause-after <stage>`, `--resume`, and `--dry-run` (there is no `--force-from`).

The default runnable pipeline scope is the full graph through `index`; the pipeline is feature-complete,
with only optional follow-ups left. Do not start the long-running site dev server
(`scripts/site.py`) by default unless the user asks for site preview or verification.

The `convert` stage parses PDF/PPTX through MinerU (failing loudly instead of degrading to local
extraction) and may run an LLM layout repair; `caption` then sends the extracted figures to the vision
model to fill in image captions before structuring.

The `structure` stage is a hard review gate. Before running `split`, the user must review
`work/structure/proposed-structure.yaml`, edit `work/structure/approved-structure.yaml`, and mark
it with a line exactly `# bookwiki: approved-structure`.

After `split`, `build_skeleton` runs `SkeletonAgent` once over every chapter's source to produce the
book-wide read-only contract (`work/skeleton.json`): a canonical glossary with each concept's
first-owning chapter, an `alias_map` (every variant → canonical), and one-line `chapter_briefs`.
`generate` injects each chapter's slice of that contract so chapters share terminology and can write
neighbour transitions; `integrate` then converges terms (rewriting `[[alias]]` to canonical) and
resolves cross-chapter concept mentions into `<PreviewLink>` tags, and audits the rendered vault for
residual term drift / unresolved cross-references (see `bookwiki/integrator/stitching.py`).

Keep agent outputs as Pydantic models. Agents do not write final Markdown; scheduler nodes write intermediate JSON and the integrator renders the vault.

Body 型文本 agent（Section/Summary/Concept 以及 chapter/concept 的 MDX repair、content rewrite）使用
MDX-direct 输出：模型返回 YAML frontmatter（结构化元数据）+ raw MDX body（正文反斜杠零转义），
再装配回原有 Pydantic output_model；quiz/card 等 item-list/结构化 agent 仍保留 JSON 输出，并由
runtime 的非法 JSON 转义修复层兜底（如 LaTeX `\mu` 这类 JSON-invalid escape 会在 instructor 解析前修复）。

The `generate` stage is agentic and runs per chapter: `SectionPlannerAgent` splits the chapter into
teaching units, `SectionAgent` writes each section's prose with flat MDX-direct frontmatter only
(section-level validate/repair with a `maxSectionRepairRounds` fallback that logs a warning and keeps
the imperfect section), then 段级知识题改由 schema 引导的 `KnowledgeQuizAgent` (JSON, flash) 按段产出，
不再进 `SectionAgent` 的 YAML frontmatter；`SectionAgent` 的 MDX-direct frontmatter 只留扁平元数据。
The assembled chapter body runs inline validation/refactor self-heal before quiz/summary: MDX,
source-ref citations, and (only when `generation.qualityCheck=true`) semantic quality are checked and
repaired via the existing chapter MDX/content agents. 应用题由 `ApplicationQuizAgent` (`deepseek-v4-pro`)
from section `application_question_requests` 专做并 inline 校验/有界修复; recall 卡片保持章级
`CardAgent` (`deepseek-v4-flash`) over the healed body, and `SummaryAgent` consumes the healed body.
When a section needs a figure the source PDF lacks, `SectionAgent` declares a `figure_request` and `SupplementImageAgent`
fills it by writing matplotlib code through LiteLLM function-calling; generated figures land under
`work/assets/generated/` and are merged into the chapter figure index so the integrator keeps them.
For **structure/flow/relationship** diagrams (flowcharts, topology skeletons, state machines, sequence,
hierarchies) Section/Concept agents may instead inline a ```mermaid fenced block directly in `body_md`
(no `figure_request`): the fumadocs site renders it via the official `remarkMdxMermaid` plugin + a client
`Mermaid` component, the bundled MDX validator compiles the fence as a plain code block (so it passes),
and the integrator's concept-link normalization stashes fenced code so it never injects `<PreviewLink>`
into a diagram. matplotlib `figure_request` stays for **quantitative** plots; mermaid is for structure —
labels must avoid LaTeX/`$...$` (mermaid renders no math).

校验下沉：`generate` / `concept_pages` now perform per-chapter and per-concept inline 自洽 loops for
raw pre-render `body_md` (MDX + 引用 + optional language-leak quality), bottoming out as warnings when
bounded repair rounds are exhausted. The macro `check` stage 退化为跨切面 checks plus 渲染态 MDX 兜底:
it still compiles rendered chapter/concept `.mdx` after `integrate` with the bundled Node validator
(`tools/mdx-validate`, using `@mdx-js/mdx` + remark-math — the same parser config as the fumadocs site)
and raises `MDX_PARSE_ERROR` for render-time breakage. `repair` keeps the MDX route for that rendered
fallback, but semantic quality repair is inline only. Quality stays default-off
(`generation.qualityCheck=false`); when off, no quality LLM call is made.

`run_plot` (the figure tool) executes LLM-written matplotlib code via **host subprocess** behind three
guardrails (AST import/call blacklist, chdir to an isolated tempdir with a scrubbed environment, and a
wall-clock timeout plus POSIX rlimits), with deterministic output (Agg backend, seeded RNG, locked
font) keyed by `sha256(code)`. This is deliberate host execution, not a hardened sandbox, because the
**threat model is a single-user local tool**: the LLM is the user's own paid (semi-trusted) DeepSeek/
Kimi model and output is rendered locally. If BookWiki is ever exposed as a multi-tenant web service,
`run_plot` MUST be upgraded to a real sandbox (Docker hardening / gVisor / E2B) and re-reviewed against
OWASP ASI05.

Agent cache misses call the configured real LLM through `bookwiki.scheduler.llm`. Configure
`DEEPSEEK_API_KEY` for `deepseek-*` models, `MOONSHOT_API_KEY` for `kimi-*` models, and
`OPENROUTER_API_KEY` for `openrouter-*` models (the default `models.vision` is
`openrouter-qwen3.6-35b-a3b`) in the process environment or in the repo root `.env`; existing
environment variables take precedence. The site's `/api/chat` route uses `BOOKWIKI_CHAT_API_KEY`
(OpenRouter). Missing keys should fail loudly rather than falling back to stub content. Tests may opt
into the explicit `BOOKWIKI_TEST_LLM=1` fake runtime.

`lg_runner` injects a single shared `LiteLLMRuntime` onto `cfg.llm_runtime` for the whole run, so every
agent reuses one LiteLLM `Router` (its tpm/rpm self-throttling and usage/cost accounting are
per-Router). The runtime accumulates token/cost usage per call and enforces `budget`
`maxCostCny` (default `70.0`; `<= 0` means unlimited), raising `BudgetExceeded` once the running total
crosses it. Per-token prices are registered on each Router deployment in CNY — the providers
(`api.moonshot.cn`, DeepSeek domestic) bill in RMB — with separate cache-hit input rates so
`cached_tokens` are priced at the discounted rate; litellm has no built-in pricing for these custom
model names, so without this registration every call would cost 0. Legacy configs using `maxCostUsd`
are migrated to `maxCostCny` (number carried over; the unit is now CNY). Chapters fan out bounded by
`maxChapterConcurrency` (default 4); sections within a chapter
also fan out, bounded by `maxSectionConcurrency` (default 3) — section inputs depend only on the static
plan, never on a sibling section body, so order is preserved by `asyncio.gather` while running in
parallel. The on-disk task cache writes atomically (temp + `os.replace`), tolerates corrupt entries by
regenerating, and keys on the agent's output JSON schema digest (so a schema field add/rename
invalidates stale entries — a one-time cost on first deploy).

The macro `repair` stage prefers DROP over fabrication: unverifiable citations, quiz items whose answer
is not among the choices, and empty-sided cards are removed (not silently re-attributed / rewritten to a
wrong answer / stuffed with placeholder text), with an audit trail in `work/logs/repair-actions.json`;
targets that exhaust `maxRepairRounds` are recorded in `work/logs/repair-exhausted.json` rather than
dropped silently. The `check` stage refuses to run when the bundled MDX validator is unavailable (no
Node / missing `node_modules`) unless `generation.allowMissingMdxValidator=true`, because a missing
validator would silently disable every MDX check. Inline repair loops keep the fewest-issue version seen
(not the last round) and discard any repair/rewrite that truncates a body below ~1/3 of its prior length.
