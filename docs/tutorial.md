# BookWiki 搭建与使用教程

这份教程带你从零把一台机器搭好,然后把**一本书**(PDF / PPTX)跑成一套学习产物(MDX vault + SQLite 索引 + 本地学习站点)。

概念性的总览见 [`../README.md`](../README.md);MinerU 单独搭建见 [`mineru-setup.md`](./mineru-setup.md);流水线设计细节见 [`../design.md`](../design.md)。

---

## 0. 前置要求

| 组件 | 版本 | 用途 |
|---|---|---|
| Python | ≥ 3.12 | 流水线运行时 |
| [uv](https://docs.astral.sh/uv/) | 任意近期版 | Python 依赖与虚拟环境管理 |
| Node.js | ≥ 20(建议 22/24) | 内置 MDX 校验器 `tools/mdx-validate`、学习站点 |
| pnpm | ≥ 10 | 学习站点构建 |
| MinerU 后端 | local 或 cloud-v4 | 解析 PDF/PPTX(见第 3 步) |
| LLM 密钥 | DeepSeek 必备 | 流水线全程的模型调用 |

> 只想先体验流水线、不接真实模型,可跳过密钥与 MinerU,用 TXT/PPTX 素材并设 `BOOKWIKI_TEST_LLM=1`(见第 8 步)。

---

## 1. 拉代码、装依赖

```bash
git clone <repo-url> bookwiki && cd bookwiki

# Python 运行时依赖
uv sync --extra runtime

# 若要在本机解析 PDF/PPTX,额外装 MinerU(很大,按需)
uv sync --extra runtime --extra mineru

# 开发/测试再加
uv sync --extra dev
```

`uv sync` 会创建 `.venv/` 并装好依赖。后续 `python scripts/...` 命令默认走这个环境(用 `uv run python ...` 可显式指定)。

---

## 2. 配置密钥(`.env`)

仓库根复制一份 `.env`:

```bash
cp .env.example .env
```

按需填写。**已存在的进程环境变量优先于 `.env`**。最少只需 DeepSeek 一项:

```dotenv
# 流水线模型(按 book.config.json 的 models 选用)
DEEPSEEK_API_KEY=sk-...
MOONSHOT_API_KEY=          # 只有用到 kimi-* 模型时才需要
OPENROUTER_API_KEY=sk-or-... # 视觉图注默认走 openrouter-qwen3.6-35b-a3b

# 用代理 / 兼容网关时覆盖 Base URL(可选)
# DEEPSEEK_API_BASE_URL=https://your-gateway/v1
```

缺密钥时各处**直接报错**,不会用占位内容兜底——这是有意为之。

---

## 3. 选择并准备 MinerU 后端

`convert` 阶段用 MinerU 解析 PDF/PPTX,失败即报错、不本地降级。两种后端二选一(完整步骤见 [`mineru-setup.md`](./mineru-setup.md)):

**A. 云端(最省事,推荐先用这个跑通)**

```dotenv
MINERU_BACKEND=cloud-v4
MINERU_API_TOKEN=your-mineru-token   # 也可用 MINERU_TOKEN
MINERU_MODEL_VERSION=vlm
```

**B. 自托管(有 GPU、要离线/批量)**

需要在 GPU 机器上起 `mineru-api`(默认 `http://127.0.0.1:8000`),然后:

```dotenv
MINERU_BACKEND=local
MINERU_API_URL=http://127.0.0.1:8000
```

跑之前可先验健康:

```bash
curl http://127.0.0.1:8000/health   # 仅 local 后端需要
```

> `MINERU_BACKEND` 接受别名:`local`/`mineru-api`/`self-hosted` 都归为 local;`cloud`/`cloud-v4` 归为 cloud-v4。纯文本/PPTX fixture 测试可以不起 MinerU。

---

## 4. 初始化一本书

```bash
python scripts/init_book.py books/calc --source ~/Downloads/calculus.pdf --title "微积分"
```

这会生成 `books/calc/` 骨架,并把源文件拷进 `books/calc/input/`:

```text
books/calc/
├── book.config.json   # 本书配置(模型、预算、生成参数)
├── book.notes.md      # 你的补充笔记,会注入生成过程
├── input/             # 源文件(已放入)
├── work/              # 中间产物(运行时生成)
├── content/docs/      # 最终 vault(运行时生成)
└── site/              # 学习站点(起站点时脚手架)
```

打开 `books/calc/book.config.json` 按需调:`budget.maxCostCny`(成本硬上限,代码默认 70,示例书设 150)、`generation` 并发与题量、`models` 各 agent 用的模型。

---

## 5. 跑流水线(到硬复核闸门)

```bash
python scripts/run.py books/calc
```

流水线 12 个节点按序跑:

```text
convert → caption → structure → split → build_skeleton → generate
        → reconcile_concepts → concept_pages → integrate → check → repair → index
```

**它会在 `structure` 之后停下**——因为这里有一道**硬复核闸门**:程序产出 `work/structure/proposed-structure.yaml` 后,要求你人工确认章节结构,才继续 `split` 及之后的(更贵的)生成阶段。

先用 `--dry-run` 看计划与成本估算心里有数:

```bash
python scripts/run.py books/calc --dry-run
```

---

## 6. 通过复核闸门

1. 审阅 `books/calc/work/structure/proposed-structure.yaml`。
2. 把它整理成 `books/calc/work/structure/approved-structure.yaml`(可直接改章节划分)。
3. 在 `approved-structure.yaml` 里加入一行(**逐字**,缺这行就不放行):

   ```text
   # bookwiki: approved-structure
   ```

4. 继续跑完剩余阶段:

   ```bash
   python scripts/run.py books/calc --resume
   ```

`generate` 是最耗时/耗钱的阶段:每章并行写正文、配图(matplotlib 或内联 mermaid)、出习题与卡片,并做内联自愈。跑完后 `content/docs/` 就是成品 vault,`site/.bookwiki/bookwiki.sqlite` 是检索索引。

---

## 7. 聚焦/重跑某个阶段

统一用 `run.py` 控制,不要直接调内部节点:

```bash
# 从某阶段开始;不带 --force 复用 task 缓存,带 --force 清缓存强制重算
python scripts/run.py books/calc --from generate
python scripts/run.py books/calc --from generate --force

# 跑到某阶段就停 / 在某阶段后暂停
python scripts/run.py books/calc --to integrate
python scripts/run.py books/calc --pause-after reconcile_concepts

# 只重跑某一章 / 某个概念页(必须配 --force)
python scripts/run.py books/calc --from generate --force --chapter ch03
python scripts/run.py books/calc --from concept_pages --force --concept derivative
```

`build_skeleton`、`reconcile_concepts`、`concept_pages`、`integrate` 这些只能经 `run.py` 访问。

---

## 8. 不接真实模型先跑通(可选)

冒烟/调试时用显式假 runtime,不打真实 LLM、不花钱:

```bash
export BOOKWIKI_TEST_LLM=1
python scripts/run.py books/<txt-or-pptx-fixture>
```

跑测试:

```bash
uv run pytest -m smoke    # CI 必跑的快速端到端
uv run pytest             # 全量
```

---

## 9. 起本地学习站点

vault 跑出来后,启动站点预览(默认不自动启动):

```bash
python scripts/site.py books/calc
```

它会把 `site-template/` 脚手架同步进 `books/calc/site/`,同步站点环境变量,然后 `pnpm install && pnpm build && pnpm start`。默认端口 3000(`BOOKWIKI_SITE_PORT` 可改),`--` 之后的参数透传给 `next start`:

```bash
python scripts/site.py books/calc -p 4000 -H 0.0.0.0
```

站点功能:**全文搜索**、**引用整页的 RAG 聊天**、**主观题/考试 LLM 判分**、**Anki 卡片导出**。聊天与判分需要各自的密钥(填进 `.env`):

```dotenv
# RAG 聊天(/api/chat)
BOOKWIKI_CHAT_API_KEY=sk-or-...
BOOKWIKI_CHAT_BASE_URL=https://openrouter.ai/api/v1
BOOKWIKI_CHAT_MODEL=google/gemma-4-31b-it

# 主观题判分(/api/evaluate)
BOOKWIKI_EVALUATE_API_KEY=sk-or-...
BOOKWIKI_EVALUATE_BASE_URL=https://openrouter.ai/api/v1
BOOKWIKI_EVALUATE_MODEL=...
```

> 站点是持久工作区:`node_modules` / `.next` / `content` 跨次保留,所以二次 `pnpm build` 是增量的。

---

## 10. 排错先看哪里

跑失败或结果异常时,优先看这些产物:

| 文件 | 看什么 |
|---|---|
| `work/logs/run-manifest.json` | 实际 token / CNY 花费(`llm_usage`)、各阶段状态 |
| `work/logs/check-report.{json,md}` | 跨切面检查与渲染态 MDX 校验结果 |
| `work/logs/chapter-split-report.md` | 章节切分是否合理 |
| `work/logs/repair-actions.json` | `repair` 删了哪些不可核验的引用/题/卡片 |
| `work/logs/repair-exhausted.json` | 修复轮数耗尽、未能修好的目标 |
| `work/skeleton.json` | 全书术语契约(canonical + alias_map) |
| `work/concepts/reconciled.json` | 跨章归并后的概念 |

常见问题:

- **`BudgetExceeded`**:成本越过 `budget.maxCostCny`。调高上限或设 `<= 0` 不限;查 `run-manifest.json` 看花在哪。
- **`MineruConversionError`**:MinerU 后端不可用。local 后端先 `curl .../health`;cloud-v4 检查 `MINERU_API_TOKEN`。
- **`MDX_PARSE_ERROR`**:渲染态 MDX 编译失败。确保装了 Node 与 `tools/mdx-validate` 的依赖;`check` 在校验器缺失时会拒跑(除非 `generation.allowMissingMdxValidator=true`)。
- **卡在复核闸门**:确认 `approved-structure.yaml` 里有逐字的 `# bookwiki: approved-structure` 那一行。
