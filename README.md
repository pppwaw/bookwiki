# BookWiki

把**一本书**的源资料(PDF / PPTX / 笔记)经一条 LLM 流水线转换成一套可学习的产物:

- 一套 Obsidian 风格的 **Fumadocs MDX vault**(`<book>/content/docs/`):分章正文、概念页、习题、记忆卡片、章节小结,`[[别名]]` 归一到 canonical 术语,跨章引用渲染成 `<PreviewLink>`。
- 一个用于全文检索与 RAG 问答的 **SQLite 索引**(`<book>/site/.bookwiki/bookwiki.sqlite`)。
- 一个本地运行的 **Next.js + Fumadocs 学习站点**:全文搜索、引用页面的 RAG 聊天、主观题/考试 LLM 判分、Anki 卡片导出。

这是一个**单用户本地工具**,没有登录、没有学习进度、没有多租户。所有 LLM 调用通过同一个 LiteLLM Router,模型输出在本地渲染。缺密钥/解析失败时**直接报错,不降级到 stub 或本地兜底**。

> **手把手搭建与使用教程见 [`docs/tutorial.md`](./docs/tutorial.md)**。设计细节见 [`design.md`](./design.md);MinerU 单独搭建见 [`docs/mineru-setup.md`](./docs/mineru-setup.md);面向 AI agent 的运行手册见 [`AGENTS.md`](./AGENTS.md) 与 [`skills/bookwiki/`](./skills/bookwiki/)。

## 快速开始

```bash
# 0. 安装依赖(uv)
uv sync --extra runtime          # 流水线运行时;解析 PDF/PPTX 还需 --extra mineru

# 1. 配置密钥:写进仓库根 .env,或导出到环境变量(已存在的环境变量优先)
cp .env.example .env             # 然后填入下方"环境变量"小节列出的键

# 2. 初始化一本书,并把源文件放进 <book>/input/
python scripts/init_book.py books/<id> --source path/to/source.pdf --title "书名"

# 3. 跑流水线(默认从头跑到 index;structure 之后是硬复核闸门,见下)
python scripts/run.py books/<id>

# 4. 复核闸门通过后,继续跑完剩余阶段
python scripts/run.py books/<id> --resume

# 5. (可选)本地预览学习站点 —— 默认不自动启动
python scripts/site.py books/<id>            # pnpm install + build + start
python scripts/site.py books/<id> -- -p 4000 # 透传额外参数给 next start
```

测试 / 冒烟可用显式假 runtime,不打真实 LLM:`export BOOKWIKI_TEST_LLM=1`。

## 流水线(12 个节点,`NODE_ORDER`)

```text
convert → caption → structure → split → build_skeleton → generate
        → reconcile_concepts → concept_pages → integrate → check → repair → index
```

| 节点 | 作用 |
|---|---|
| `convert` | MinerU VLM 解析 PDF/PPTX(失败即报错,不本地降级)+ 可选 LLM 版面修复 → `work/sources_md/` + `work/source_refs/` |
| `caption` | 把抽出的图送 `models.vision` 视觉模型补图注(默认 `openrouter-qwen3.6-35b-a3b`);同页多图合并为一次调用 |
| `structure` | `StructureAgent` 产 `work/structure/proposed-structure.yaml`(**硬复核闸门**,见下) |
| `split` | `ChapterSplitAgent` 按主题把片段对齐到章节 → `work/chapter_sources/<id>/` |
| `build_skeleton` | 流式:逐章并行抽概念候选 → 按章序确定性折叠成全书只读契约 `work/skeleton.json`(canonical 术语表 + `alias_map` + `chapter_briefs`) |
| `generate` | 每章 agentic 流水线:`SectionPlannerAgent` → `SectionAgent`(MDX-direct)+ `KnowledgeQuizAgent` + 段级修复 + 配图(`SupplementImageAgent` matplotlib / 内联 mermaid)→ 装配 → 内联自愈 → `ApplicationQuizAgent` / `CardAgent` / `SummaryAgent`,只产 `work/agent_results/*.json` |
| `reconcile_concepts` | `ConceptReconcileAgent` 跨章归并概念 → `work/concepts/reconciled.json` + `alias_map.json` |
| `concept_pages` | `ConceptAgent` 每个唯一概念出一页(内联自愈) |
| `integrate` | 渲染 vault MDX,`[[alias]]` 归一到 canonical,跨章引用解析成 `<PreviewLink>`,审计术语漂移 |
| `check` | 跨切面检查 + 用内置 Node 校验器 `tools/mdx-validate` 编译渲染态 MDX(与站点同一套 `@mdx-js/mdx` + remark-math 解析) |
| `repair` | 确定性"宁删不假造":删无法核验的引用 / 答案不在选项里的题 / 空卡片,审计写 `work/logs/repair-actions.json` |
| `index` | 重建 `site/.bookwiki/bookwiki.sqlite` |

校验下沉:`generate` / `concept_pages` 已对每章/每概念做内联自愈,宏观 `check` 只兜底渲染态破坏;语义质量检查默认关(`generation.qualityCheck=false`,关时不发质量 LLM 调用)。内联修复保留"问题最少"的版本,并丢弃把正文截断到原长 1/3 以下的修复。

## 硬复核闸门

`structure` 之后、`split` 之前**强制人工复核**:审 `work/structure/proposed-structure.yaml`,编辑出 `work/structure/approved-structure.yaml`,并加入一行(逐字):

```text
# bookwiki: approved-structure
```

缺这一行则停下要求复核,不绕过闸门。

## 阶段控制

聚焦单段时统一用 `run.py` 控制:

- `--from <stage>`:从某阶段开始;不带 `--force` 时保留 task 缓存复用 LLM 结果,带 `--force` 时清缓存强制重算。
- `--to <stage>`:跑到某阶段后停。
- `--pause-after <stage>`:在某阶段(可逗号分隔多个)后暂停。
- `--resume`:从最近 checkpoint 继续。
- `--dry-run`:打印计划与成本估算,不执行。
- `--chapter <id>`:仅配合 `--from generate --force` 重跑指定章(可重复 / 逗号分隔)。
- `--concept <id>`:仅配合 `--from concept_pages --force` 重跑指定概念页。

`build_skeleton`、`reconcile_concepts`、`concept_pages`、`integrate` 只能经 `run.py` 访问(如 `--pause-after reconcile_concepts`)。默认不要启动长驻站点 `scripts/site.py`,除非用户明确要求预览或验证。

## 学习站点

`scripts/site.py books/<id>` 把 `site-template/` 脚手架同步进 `<book>/site/`(幂等,跨次保留 `node_modules` / `.next` / 内容,使 `pnpm build` 增量),同步站点环境变量,然后 `pnpm install && pnpm build && pnpm start`;`--` 之后的参数透传给 `next start`。站点功能:

- **全文搜索**(`/api/search`):基于 SQLite 索引。
- **RAG 聊天**(`/api/chat`):可引用单个概念/章节整页;用 `BOOKWIKI_CHAT_*` 配置模型。
- **考试 / 主观题判分**(`/api/evaluate`):流式心跳保活,LLM 逐点给分;用 `BOOKWIKI_EVALUATE_*` 配置模型。
- **Anki 导出**(`/api/anki`):把记忆卡片导出为 Anki 卡组。

## 书目录布局(`books/<id>/`)

```text
book.config.json   # 本书配置(见下)
book.notes.md      # 作者笔记,作为 notesPath 注入生成
input/             # 放源文件(PDF / PPTX)
work/              # 全部中间产物:sources_md/ source_refs/ structure/ skeleton.json
                   #   concepts/ agent_results/ assets/ logs/ …
content/docs/      # 最终 vault(Fumadocs MDX)
site/              # 由 site-template 脚手架而来的 Next.js 站点;索引在 site/.bookwiki/bookwiki.sqlite
```

## 配置要点(`book.config.json`)

- `budget.maxCostCny`:成本硬上限,代码默认 `70.0`(示例书设为 `150.0`);`<= 0` 为不限,越线抛 `BudgetExceeded`。每次运行后的实际花费写入 `work/logs/run-manifest.json` 的 `llm_usage.total_cost_cny`,分阶段明细见 `llm_usage.stages`。
- `generation`:`quizPerChapter=5`、`cardsPerChapter=8`、`maxChapterConcurrency=4`、`maxSectionConcurrency=3`、`maxRepairRounds=3`、`qualityCheck=false`、`maxQualityRounds=2`、`allowMissingMdxValidator=false`;另有 `sourceLayoutRepair`(版面修复 auto/置信度阈值)、`visionCaption`(图注并发/上限)子配置。
- `models`:按 agent 选模型。`deepseek-*` 走 `DEEPSEEK_API_KEY`,`kimi-*` 走 `MOONSHOT_API_KEY`,`openrouter-*` 走 `OPENROUTER_API_KEY`。API Base URL 可用 `DEEPSEEK_API_BASE_URL` / `MOONSHOT_API_BASE_URL` / `OPENROUTER_API_BASE_URL` 覆盖(短别名 `*_API_BASE` 亦可);Moonshot 默认 `https://api.moonshot.cn/v1`,OpenRouter 默认 `https://openrouter.ai/api/v1`。

## 环境变量

写进仓库根 `.env`(已存在的进程环境变量优先),完整模板见 [`.env.example`](./.env.example)。

| 变量 | 用途 |
|---|---|
| `DEEPSEEK_API_KEY` | `deepseek-*` 模型 |
| `MOONSHOT_API_KEY` | `kimi-*` 模型(可选) |
| `OPENROUTER_API_KEY` | `openrouter-*` 模型;默认视觉图注 `openrouter-qwen3.6-35b-a3b` |
| `*_API_BASE_URL` | 用代理 / 兼容网关时覆盖各家 Base URL |
| `MINERU_BACKEND` / `MINERU_API_*` / `MINERU_MODEL_VERSION` | `convert` 阶段 MinerU 解析后端(本地或云端)配置 |
| `BOOKWIKI_CHAT_API_KEY` / `BOOKWIKI_CHAT_BASE_URL` / `BOOKWIKI_CHAT_MODEL` | 站点 RAG 聊天 |
| `BOOKWIKI_EVALUATE_API_KEY` / `BOOKWIKI_EVALUATE_BASE_URL` / `BOOKWIKI_EVALUATE_MODEL` | 站点主观题判分 |
| `BOOKWIKI_SITE_PORT` | 站点端口(默认 3000) |
| `BOOKWIKI_TEST_LLM=1` | 测试/冒烟:启用显式假 runtime,不打真实 LLM |

缺密钥时各处都**直接报错**,不会用占位内容兜底。

## 排错先看

`work/logs/run-manifest.json`(含 `llm_usage` 实际 token / CNY 花费)、`work/logs/check-report.{json,md}`、`work/logs/chapter-split-report.md`、`work/logs/repair-actions.json`、`work/logs/repair-exhausted.json`、`work/skeleton.json`、`work/concepts/reconciled.json`、`work/agent_results/*.json`。

## 开发

```bash
uv sync --extra dev
pytest -m smoke      # CI 必跑的快速端到端冒烟
pytest               # 全量
ruff check . && ruff format .
```
