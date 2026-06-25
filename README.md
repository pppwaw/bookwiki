# BookWiki

把**一本书**的源资料（PDF / PPTX / 笔记）转换成一套可学习的产物：

- 一个 Obsidian 风格的 **Fumadocs MDX vault**（`content/docs/`）
- 一个用于全文检索与 RAG 问答的 **SQLite 索引**（`site/.bookwiki/bookwiki.sqlite`）
- 一个本地运行的 **Next.js + Fumadocs 学习站点**

这是一个**单用户本地工具**，没有登录、没有学习进度、没有多租户。所有 LLM 调用通过 LiteLLM Router，模型输出在本地渲染。

> 设计细节见 [`design.md`](./design.md)；面向 AI agent 的运行手册见 [`AGENTS.md`](./AGENTS.md) 与 [`skills/bookwiki/`](./skills/bookwiki/)。

## 快速开始

```bash
# 1. 配置密钥（缺失时直接报错，不降级到 stub）
export DEEPSEEK_API_KEY="..."     # deepseek-* 模型
export MOONSHOT_API_KEY="..."     # kimi-* 模型（可选）
export OPENROUTER_API_KEY="..."   # openrouter-* 模型；默认视觉图注 openrouter-qwen3.6-35b-a3b
# 可选：使用代理 / 兼容网关时覆盖 API Base URL
export DEEPSEEK_API_BASE_URL="https://.../v1"
export MOONSHOT_API_BASE_URL="https://.../v1"
export OPENROUTER_API_BASE_URL="https://openrouter.ai/api/v1"

# 2. 初始化一本书，并把源文件放进 input/
python scripts/init_book.py books/<id> --source path/to/source.pdf

# 3. 跑完整流水线（默认跑到 index）
python scripts/run.py books/<id>

# 4. 结构复核闸门通过、或中断后续跑
python scripts/run.py books/<id> --resume
```

测试/冒烟可用显式假 runtime：`export BOOKWIKI_TEST_LLM=1`。

## 流水线（12 个节点，`NODE_ORDER`）

```text
convert → caption → structure → split → build_skeleton → generate
        → reconcile_concepts → concept_pages → integrate → check → repair → index
```

| 节点 | 作用 |
|---|---|
| `convert` | MinerU VLM 解析 PDF/PPTX（失败即报错，不本地降级）+ 可选 LLM 版面修复 → `work/sources_md/` + `work/source_refs/` |
| `caption` | 把抽出的图送配置的 `models.vision` 视觉模型补图注（默认 `openrouter-qwen3.6-35b-a3b`） |
| `structure` | `StructureAgent` 产 `work/structure/proposed-structure.yaml`（**硬复核闸门**，见下） |
| `split` | `ChapterSplitAgent` 按主题把片段对齐到章节 → `work/chapter_sources/<id>/` |
| `build_skeleton` | `SkeletonAgent` 通览全书产只读契约 `work/skeleton.json`（canonical 术语表 + `alias_map` + `chapter_briefs`） |
| `generate` | 每章 section 流水线：`SectionPlannerAgent` → `SectionAgent` + `KnowledgeQuizAgent` + 段级修复 + 配图（`SupplementImageAgent`/`run_plot`）→ 装配 → 内联自愈 → `ApplicationQuizAgent` / `CardAgent` / `SummaryAgent`，只产 `work/agent_results/*.json` |
| `reconcile_concepts` | `ConceptReconcileAgent` 跨章归并概念 → `work/concepts/reconciled.json` + `alias_map.json` |
| `concept_pages` | `ConceptAgent` 每个唯一概念出一页（内联自愈） |
| `integrate` | 渲染 vault MDX，`[[alias]]` 归一到 canonical，跨章引用解析成 `<PreviewLink>`，审计术语漂移 |
| `check` | 跨切面检查 + 用内置 Node 校验器 `tools/mdx-validate` 编译渲染态 MDX |
| `repair` | 确定性"宁删不假造"：删无法核验的引用 / 答案不在选项里的题 / 空卡片，审计写 `work/logs/repair-actions.json` |
| `index` | 重建 `site/.bookwiki/bookwiki.sqlite` |

校验下沉：`generate` / `concept_pages` 已对每章/每概念做内联自愈，宏观 `check` 只兜底渲染态破坏；语义质量默认关（`generation.qualityCheck=false`）。

## 硬复核闸门

`structure` 之后、`split` 之前**强制人工复核**：审 `work/structure/proposed-structure.yaml`，编辑 `work/structure/approved-structure.yaml`，并加入一行：

```text
# bookwiki: approved-structure
```

缺这一行则停下要求复核，不绕过闸门。

## 阶段控制

聚焦单段时统一用 `run.py` 控制：`--from <stage>`（不带 `--force` 时保留 task 缓存复用 LLM 结果，带 `--force` 时清缓存强制重算）、`--to <stage>`、`--pause-after <stage>`、`--resume`、`--dry-run`。

`build_skeleton`、`reconcile_concepts`、`concept_pages`、`integrate` 只能经 `run.py` 访问（如 `--pause-after reconcile_concepts`）。

默认不要启动长驻站点 `scripts/site.py`，除非用户明确要求预览或验证。

## 配置要点（`book.config.json`）

- `budget.maxCostCny`：成本硬上限，默认 `70.0`；`<= 0` 为不限，越线抛 `BudgetExceeded`。每次运行后的实际花费写入 `work/logs/run-manifest.json` 的 `llm_usage.total_cost_cny`，分阶段明细见 `llm_usage.stages`。
- `generation`：`quizPerChapter=5`、`cardsPerChapter=8`、`maxChapterConcurrency=4`、`maxSectionConcurrency=3`、`maxRepairRounds=3`、`qualityCheck=false`、`maxQualityRounds=2`、`allowMissingMdxValidator=false`；`VisionCaptionAgent` 统一按 `images` 列表处理图注，同一 `source_ref` 页上的多图自动合并为一次视觉模型调用。
- `models`：按 agent 选模型（`deepseek-*` 走 `DEEPSEEK_API_KEY`，`kimi-*` 走 `MOONSHOT_API_KEY`，`openrouter-*` 走 `OPENROUTER_API_KEY`）。API Base URL 可用 `DEEPSEEK_API_BASE_URL` / `MOONSHOT_API_BASE_URL` / `OPENROUTER_API_BASE_URL` 覆盖，短别名 `*_API_BASE` 也可用；Moonshot 默认 `https://api.moonshot.cn/v1`，OpenRouter 默认 `https://openrouter.ai/api/v1`。

## 排错先看

`work/logs/run-manifest.json`（含 `llm_usage` 实际 token / CNY 花费）、`work/logs/check-report.{json,md}`、`work/logs/chapter-split-report.md`、`work/logs/repair-actions.json`、`work/logs/repair-exhausted.json`、`work/skeleton.json`、`work/concepts/reconciled.json`、`work/agent_results/*.json`。
