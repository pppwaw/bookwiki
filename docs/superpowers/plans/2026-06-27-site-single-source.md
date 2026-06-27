# Site 单一事实来源重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `book_dir/site` 成为唯一事实来源——integrate 直接把渲染好的 mdx 写进 `site/content/docs` 并完成数学归一化，check/repair 都在真 site 上操作，`scripts/site.py` 退化为只负责 scaffold + 启动。

**Architecture:** 核心机制是把 `BookConfig.content_dir` 从 `book_dir/content/docs` 改成 `book_dir/site/content/docs`，于是所有 `cfg.content_dir` 引用（integrate/check/repair/index）自动跟随到 site 内。`materialize_site` 拆成 `scaffold_site_template`（拷模板/assets/graph，**不碰 content/docs**，且保留 `node_modules`/`.next` 让 build 增量复用）+ 渲染（由 integrate 负责）。数学归一化从 `materialize` 阶段前移到 integrate 末尾。

**Tech Stack:** Python 3.12、LangGraph pipeline、Next.js + fumadocs 站点（pnpm）、pytest。

## Global Constraints

- ESM 优先（前端/Node 侧），Python 侧避免 class 写法、用现有函数式风格。
- 禁止 mock/fallback：不得用兜底数据或回退路径掩盖契约问题；找根因。
- MDX 数学定界符严格 `$`/`$$` 契约（见 memory `math-delimiter-contract`），归一化逻辑不得破坏逐字引用例外。
- 所有路径用 `pathlib.Path`，相对路径用 `_rel(...)` 记录。
- 改完跑 `ruff`/`pytest`，提交信息中文、Co-Author 不含 noreply@anthropic.com。

---

### Task 1: 重复物化保留依赖（PRESERVE node_modules/.next）

**问题:** `materialize_site` 现在先删 site 内所有非 PRESERVE 文件再拷模板，每轮都删 `node_modules`/`.next`，导致 check 每次都要重装、build 无法增量。site 成为持久工作区后必须保留这些生成物。

**Files:**
- Modify: `scripts/site.py:29-41`（`PRESERVE_SITE_NAMES` / `SKIP_TEMPLATE_NAMES`）
- Test: `tests/test_config_language.py`（已有 `test_materialize_site_removes_node_modules_by_default` 等需改语义）

**Interfaces:**
- Produces: `PRESERVE_SITE_NAMES` 新增 `node_modules`、`.next`、`.source`、`pnpm-lock.yaml`、`tsconfig.tsbuildinfo`、`content`。

- [ ] **Step 1: 改测试预期——物化保留 node_modules**

把 `tests/test_config_language.py::test_materialize_site_removes_node_modules_by_default` 改名/改语义为 `test_materialize_site_preserves_node_modules`：

```python
def test_materialize_site_preserves_node_modules(tmp_path):
    book_dir = _make_book_with_content(tmp_path)  # 既有 helper：建 content/docs + mdx
    cfg = load_config(book_dir)
    nm = cfg.site_dir / "node_modules"
    nm.mkdir(parents=True)
    (nm / "marker").write_text("keep-me", encoding="utf-8")

    scaffold_site_template(cfg)  # Task 3 引入；本步先保留旧名 materialize_site

    assert (nm / "marker").read_text(encoding="utf-8") == "keep-me"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_config_language.py::test_materialize_site_preserves_node_modules -v`
Expected: FAIL（当前逻辑会删掉 node_modules）

- [ ] **Step 3: 扩充 PRESERVE 集合**

```python
PRESERVE_SITE_NAMES = {
    ".bookwiki",
    ".env.local",
    "tsconfig.tsbuildinfo",
    "node_modules",
    ".next",
    ".source",
    "pnpm-lock.yaml",
    "content",  # content/docs 由 integrate 渲染，物化阶段不得清空
}
```

- [ ] **Step 4: 运行确认通过 + 全量 site 测试**

Run: `pytest tests/test_config_language.py -v -k materialize`
Expected: PASS（含 preserves_node_modules、preserves_local_env_file）

- [ ] **Step 5: 提交**

```bash
git add scripts/site.py tests/test_config_language.py
git commit -m "refactor(site): 物化保留 node_modules/.next/content 等生成物，为持久工作区铺路"
```

---

### Task 2: 数学归一化前移到 integrate（覆盖全部 mdx 写入）

**问题:** 现在 integrate 只对 chapter body / key_points / concept body 调 `normalize_mdx_math`（`nodes.py:3651,3688,3733`），`index.mdx`（3773）等未覆盖；缺口由 `materialize` 的 `_normalize_site_mdx` 兜底。site 单一来源后这层兜底要删，必须让 integrate 自己保证全覆盖。

**Files:**
- Modify: `bookwiki/pipeline/nodes.py`（integrate_node 末尾、写完所有文件后、`audit_stitching` 之前）
- Test: `tests/test_pipeline_nodes.py`

**Interfaces:**
- Consumes: `normalize_mdx_math`（已在 `nodes.py:68` 导入）。
- Produces: integrate 结束时 `content_dir` 下每个 `*.mdx` 都已归一化。

- [ ] **Step 1: 写失败测试——integrate 后 index.mdx 也被归一化**

```python
def test_integrate_normalizes_all_mdx_including_index(tmp_path, minimal_integrate_state):
    cfg, state = minimal_integrate_state  # 既有 fixture
    # 注入一个首页描述里带未归一化数学的标题，断言落盘后已归一化
    out = asyncio.run(integrate_node(state, cfg))
    index_text = (cfg.content_dir / "index.mdx").read_text(encoding="utf-8")
    assert normalize_mdx_math(index_text) == index_text  # 幂等 == 已归一化
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_pipeline_nodes.py::test_integrate_normalizes_all_mdx_including_index -v`
Expected: FAIL

- [ ] **Step 3: integrate 末尾统一归一化**

在 integrate_node 写完 index.mdx/meta.json、`audit_stitching` 调用之前插入（把原 `_normalize_site_mdx` 的职责搬进来）：

```python
    # 单一来源：在此统一归一化所有落盘 mdx，删除 materialize 阶段的二次归一化。
    for mdx_path in content_dir.rglob("*.mdx"):
        text = mdx_path.read_text(encoding="utf-8")
        normalized = normalize_mdx_math(text)
        if normalized != text:
            write_text(mdx_path, normalized)
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_pipeline_nodes.py -v -k integrate`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add bookwiki/pipeline/nodes.py tests/test_pipeline_nodes.py
git commit -m "refactor(integrate): 落盘 mdx 统一数学归一化，覆盖 index.mdx 等全部写入"
```

---

### Task 3: 拆出 scaffold_site_template，删除 _normalize_site_mdx 与 content 拷贝

**问题:** `materialize_site` 既拷模板又拷 content 还做归一化。单一来源后 content 由 integrate 直接产出，模板物化只需"拷框架 + assets + concept-graph"。

**Files:**
- Modify: `scripts/site.py:55-108`（`materialize_site` → `scaffold_site_template`，删 `_normalize_site_mdx`）
- Test: `tests/test_config_language.py`、`tests/test_e2e_smoke.py`

**Interfaces:**
- Produces: `def scaffold_site_template(book: BookConfig | str | Path) -> Path` —— 拷模板、`work/assets`→`public/bookwiki-assets`、`work/concept-graph.json`→`public/concept-graph.json`，**不创建/不清空 `site/content/docs`**，返回 `site_dir`。保留旧名 `materialize_site = scaffold_site_template` 作为别名一轮，避免一次性改爆所有调用方。

- [ ] **Step 1: 写失败测试——scaffold 不动 content/docs**

```python
def test_scaffold_keeps_existing_content_docs(tmp_path):
    book_dir = _make_book_with_content(tmp_path)
    cfg = load_config(book_dir)
    docs = cfg.site_dir / "content" / "docs"
    docs.mkdir(parents=True)
    (docs / "sentinel.mdx").write_text("---\ntitle: x\n---\n", encoding="utf-8")

    scaffold_site_template(cfg)

    assert (docs / "sentinel.mdx").exists()  # integrate 的产物不被物化清掉
    assert (cfg.site_dir / "package.json").exists()  # 模板已就位
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_config_language.py::test_scaffold_keeps_existing_content_docs -v`
Expected: FAIL（`AttributeError` 或旧逻辑清掉 sentinel）

- [ ] **Step 3: 重写函数**

```python
def scaffold_site_template(book: BookConfig | str | Path) -> Path:
    cfg = book if isinstance(book, BookConfig) else load_site_config(book)
    site_dir = cfg.site_dir
    site_dir.mkdir(parents=True, exist_ok=True)

    # 覆盖式拷模板（保留 PRESERVE_SITE_NAMES 内的生成物，尤其 content/node_modules）
    for child in TEMPLATE_DIR.iterdir():
        if child.name in SKIP_TEMPLATE_NAMES:
            continue
        target = site_dir / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*SKIP_TEMPLATE_NAMES))
        else:
            shutil.copy2(child, target)

    # assets 与 concept-graph 仍由物化阶段搬运（integrate 已写到 work/）
    _copy_tree(cfg.work_dir / "assets", site_dir / "public" / "bookwiki-assets")
    _copy_file(cfg.work_dir / "concept-graph.json", site_dir / "public" / "concept-graph.json")
    return site_dir


# 兼容别名（Task 6/7 改完调用方后删除）
materialize_site = scaffold_site_template
```

并删除 `_normalize_site_mdx`（已由 Task 2 接管）。新增 `_copy_tree`/`_copy_file` 小工具（覆盖式拷贝，目标存在先删）。

> 注意：模板 `content/` 里若带 `meta.json` 模板，`SKIP_TEMPLATE_NAMES` 需补 `content`，避免覆盖 integrate 渲染的 docs。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_config_language.py -v -k "scaffold or materialize"`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add scripts/site.py tests/test_config_language.py
git commit -m "refactor(site): materialize 拆为 scaffold_site_template，移除 content 拷贝与二次归一化"
```

---

### Task 4: content_dir 指向 site 内 + 修硬编码路径

**问题:** 把唯一开关 `BookConfig.content_dir` 指到 `site/content/docs`，下游全部跟随。再修两处绕过该属性的硬编码。

**Files:**
- Modify: `bookwiki/scheduler/config.py:91-93`
- Modify: `bookwiki/indexer/sqlite_builder.py:47-49`（`rebuild_sqlite` 硬编码 `book_dir/content/docs`）
- Modify: `bookwiki/scheduler/lg_runner.py:235`（outputs 记录）
- Test: `tests/test_pipeline_nodes.py`、`tests/test_m6_indexer.py`

**Interfaces:**
- Produces: `cfg.content_dir == cfg.site_dir / "content" / "docs"`。

- [ ] **Step 1: 写失败测试**

```python
def test_content_dir_lives_inside_site(tmp_path):
    cfg = load_config(_make_book_with_content(tmp_path))
    assert cfg.content_dir == cfg.site_dir / "content" / "docs"
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_pipeline_nodes.py::test_content_dir_lives_inside_site -v`
Expected: FAIL

- [ ] **Step 3: 改属性 + 硬编码**

`config.py`：
```python
    @property
    def content_dir(self) -> Path:
        return self.site_dir / "content" / "docs"
```

`sqlite_builder.py::rebuild_sqlite`：
```python
    return build_sqlite_index(
        book_dir / "site" / "content" / "docs",
        book_dir / "site" / ".bookwiki" / "bookwiki.sqlite",
    )
```

`lg_runner.py:235` 无需改 key，仅确认 `cfg.content_dir` 现在打印的是 site 内路径（值自动更新）。

- [ ] **Step 4: 运行确认通过 + indexer 测试**

Run: `pytest tests/test_pipeline_nodes.py::test_content_dir_lives_inside_site tests/test_m6_indexer.py -v`
Expected: PASS（indexer 测试用 tmp_path 显式传 content_dir，不受影响）

- [ ] **Step 5: 提交**

```bash
git add bookwiki/scheduler/config.py bookwiki/indexer/sqlite_builder.py
git commit -m "refactor(config): content_dir 指向 site/content/docs，唯一事实来源"
```

---

### Task 5: integrate 开头 scaffold 模板，确保渲染前框架就位

**问题:** content_dir 现在在 site 内，但 integrate 直接 `ensure_dir(content_dir)` 写文件，若 site 模板（package.json、app/ 等）未就位则站点跑不起来。integrate 必须在渲染前先 scaffold。

**Files:**
- Modify: `bookwiki/pipeline/nodes.py:3542-3551`（integrate_node 开头）
- Test: `tests/test_pipeline_nodes.py`

**Interfaces:**
- Consumes: `scaffold_site_template`（Task 3）。
- Produces: integrate 后 `site/package.json` 与 `site/content/docs/index.mdx` 同时存在。

- [ ] **Step 1: 写失败测试**

```python
def test_integrate_scaffolds_site_framework(tmp_path, minimal_integrate_state):
    cfg, state = minimal_integrate_state
    asyncio.run(integrate_node(state, cfg))
    assert (cfg.site_dir / "package.json").exists()
    assert (cfg.content_dir / "index.mdx").exists()
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_pipeline_nodes.py::test_integrate_scaffolds_site_framework -v`
Expected: FAIL（site 无 package.json）

- [ ] **Step 3: integrate_node 开头注入 scaffold**

```python
async def integrate_node(state: State, cfg: BookConfig) -> State:
    from scripts.site import scaffold_site_template
    scaffold_site_template(cfg)            # 先铺 Next.js 框架（幂等，保留生成物）
    content_dir = ensure_dir(cfg.content_dir)
    chapters_dir = content_dir / "chapters"
    ...
```

> 顺序保证：scaffold 因 PRESERVE 含 `content` 不会清空 docs；随后 integrate 自己重建 chapters_dir、清旧概念，正常渲染。assets/concept-graph 由 scaffold 搬运——但 integrate 后续才写 `work/concept-graph.json`，故把 graph 的搬运挪到 integrate 末尾（写完 graph 后再 `_copy_file`），scaffold 只负责框架与 assets。

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_pipeline_nodes.py -v -k integrate`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add bookwiki/pipeline/nodes.py
git commit -m "feat(integrate): 渲染前 scaffold site 框架，并在末尾搬运 concept-graph 到 public"
```

---

### Task 6: check 在真 site 上跑校验，不再临时物化

**问题:** `_site_typecheck_issues` 现在每轮 `materialize_site(cfg)` 重建临时 site + 无条件 `pnpm install`。site 已是真源后，直接在 `cfg.site_dir` 上跑；`node_modules` 已在则跳过 install。

**Files:**
- Modify: `bookwiki/pipeline/nodes.py:3891-3972`（`_site_typecheck_issues`）
- Test: `tests/test_quality_pipeline.py` 或 `tests/test_pipeline_nodes.py`（mock subprocess）

**Interfaces:**
- Consumes: `cfg.site_dir`（已由 integrate scaffold + 渲染就绪）。

- [ ] **Step 1: 写失败测试——check 不调 materialize、复用 node_modules**

```python
def test_site_typecheck_skips_install_when_node_modules_present(monkeypatch, tmp_path):
    cfg = load_config(_make_book_with_site(tmp_path))  # site 内含 content/docs + node_modules
    calls = []
    monkeypatch.setattr("bookwiki.pipeline.nodes.subprocess.run",
                        lambda *a, **k: calls.append(a[0]) or _ok())
    _site_typecheck_issues(cfg)
    assert not any("install" in " ".join(c) for c in calls)  # 复用现有依赖
    assert all("materialize" not in repr(c) for c in calls)
```

- [ ] **Step 2: 运行确认失败**

Run: `pytest tests/test_pipeline_nodes.py::test_site_typecheck_skips_install_when_node_modules_present -v`
Expected: FAIL

- [ ] **Step 3: 改 `_site_typecheck_issues`**

去掉 `from scripts.site import materialize_site` 与 `site_dir = materialize_site(cfg)`，改为：

```python
    site_dir = cfg.site_dir
    if not (site_dir / "node_modules").exists():
        install = subprocess.run([pnpm, "install"], cwd=site_dir, env=_site_typecheck_env(cfg),
                                 capture_output=True, text=True)
        if install.returncode != 0:
            return [Issue(severity="error", code="SITE_TYPECHECK_ERROR",
                          message=f"pnpm install failed: {install.stderr[-2000:]}",
                          owner_task_id="site:typecheck")]
    types = subprocess.run([pnpm, "run", "types:check"], cwd=site_dir, env=_site_typecheck_env(cfg),
                           capture_output=True, text=True)
    # ...（保留原有错误解析逻辑）
```

- [ ] **Step 4: 运行确认通过**

Run: `pytest tests/test_pipeline_nodes.py -v -k typecheck`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add bookwiki/pipeline/nodes.py tests/test_pipeline_nodes.py
git commit -m "refactor(check): 直接在持久 site 上 types:check，依赖已装则跳过 install"
```

> **可选扩展（需用户最终拍板，见计划末「待决」）:** 在 types:check 之后追加 `pnpm build`（含 Shiki 渲染）以捕获运行时渲染错误（如 ` ```quiz ` 触发的 ShikiError），产出 `SITE_BUILD_ERROR` issue 进入 repair loop。成本：每轮多一次完整 build。

---

### Task 7: scripts/site.py main 瘦身为 scaffold + 启动

**Files:**
- Modify: `scripts/site.py:234-248`（`main`），删除别名 `materialize_site`
- Modify: `bookwiki/pipeline/nodes.py`、`tests/*`（把残留 `materialize_site` 调用改 `scaffold_site_template`）

**Interfaces:**
- Consumes: `scaffold_site_template`、`sync_site_env`、`sync_public_book_id`。

- [ ] **Step 1: 全仓替换调用名**

Run: `grep -rn "materialize_site" --include="*.py" .`
把每个调用点（nodes.py、tests）改成 `scaffold_site_template`，删 site.py 末尾别名。

- [ ] **Step 2: main 改为不渲染 content、仅 scaffold + 启动**

```python
def main() -> None:
    parser = book_arg_parser("Start the BookWiki Next.js demo site.")
    args = parser.parse_args()
    cfg = load_site_config(args.book_dir)
    if not cfg.content_dir.exists():
        raise FileNotFoundError(
            f"site content not rendered yet: {cfg.content_dir}. Run the pipeline (integrate) first."
        )
    site_dir = scaffold_site_template(cfg)
    sync_site_env(site_dir)
    sync_public_book_id(site_dir, cfg.book_id)
    env = os.environ.copy()
    env["BOOKWIKI_SITE_LANGUAGE"] = cfg.language
    env.setdefault("NODE_OPTIONS", "--max-old-space-size=4096")
    if not (site_dir / "node_modules").exists():
        subprocess.run(["pnpm", "install"], cwd=site_dir, env=env, check=True)
    subprocess.run(["pnpm", "run", "build"], cwd=site_dir, env=env, check=True)
    subprocess.run(["pnpm", "start"], cwd=site_dir, env=env, check=True)
```

- [ ] **Step 3: 跑全量单测**

Run: `pytest tests/ -q`
Expected: PASS（或仅剩需在 Task 8 调整的 e2e）

- [ ] **Step 4: 提交**

```bash
git add scripts/site.py bookwiki/pipeline/nodes.py tests/
git commit -m "refactor(site): main 仅 scaffold + 启动，content 渲染交还 pipeline"
```

---

### Task 8: 修复受影响测试与一次真实 e2e

**Files:**
- Modify: `tests/test_e2e_smoke.py:16,72`、`tests/test_config_language.py` 中硬编码 `content/docs` 处
- Test: 实跑一次 `circuits`

- [ ] **Step 1: 更新 e2e 期望**——`materialize_site`→`scaffold_site_template`，断言 `site/content/docs/index.mdx` 由 integrate 产出（测试需先跑 integrate 或预置 docs）。

- [ ] **Step 2: 跑全量单测**

Run: `pytest tests/ -q`
Expected: PASS

- [ ] **Step 3: 真实重跑一本书验证闭环**

Run: `python -m bookwiki.scheduler.lg_runner --book books/circuits --resume`（按项目实际入口）
Expected: integrate 渲染进 `books/circuits/site/content/docs`；check 在真 site 跑；`pnpm run start` 能起站点且 `node_modules` 未被重装。

- [ ] **Step 4: 提交**

```bash
git add tests/
git commit -m "test(site): 适配单一来源后的 e2e 与物化语义"
```

---

## 附带的独立修复（与 site 重构正交，可单独 PR 先行）

### Task A: owner_task_id 用真实 chapter_id（修 30→1 碰撞 + repair 反查落空）

**问题:** check 用 `path.stem`（`index`/`exam`）当 owner_task_id 第一段，导致 30 个 `exam.mdx` 塌成一个 target，且 repair 拿 `exam` 去查 `agent_results["exam"]` 必然落空 → exhausted。

**Files:**
- Modify: `bookwiki/pipeline/nodes.py`（`check_node` 生成 owner_task_id 处：688/4048/4057/4066/4077/4086/4095/4105；`_owner_artifact_path`/`_repair_mdx_file` 反查）
- Test: `tests/test_pipeline_nodes.py`

- [ ] **Step 1: 失败测试**——两个不同章节的 `exam.mdx` 缺 quiz，应产出两个不同 target（含章节目录），且能反查回各自文件。
- [ ] **Step 2:** 把 owner_task_id 第一段从 `path.stem` 改为该文件所属 chapter_id（由 `path.relative_to(cfg.content_dir/"chapters")` 的首段目录名映射到 `agent_results` 的 key），`_owner_artifact_path` 据此反查。
- [ ] **Step 3:** 跑 `pytest tests/test_pipeline_nodes.py -v -k owner` → PASS。
- [ ] **Step 4:** commit `fix(check): owner_task_id 带章节 id，消除同名文件碰撞与 repair 反查落空`。

### Task B: 消灭 ` ```quiz ` 代码围栏（生成侧 + 检测侧）

**问题:** `section_agent` 把 mermaid 围栏段（109-122）与 quiz 段（124）相邻排版，诱发 LLM 把 `<QuizBlock>` 包进 ` ```quiz `；check 因其是合法 fenced code 且含 `<QuizBlock` 字样而双重漏检，最终在站点 Shiki 渲染时 `Language quiz not found`。

**Files:**
- Modify: `bookwiki/agents/section_agent.py:124-171`（prompt 明确禁止围栏）
- Modify: `bookwiki/pipeline/nodes.py`（check 增加围栏语言白名单检测）
- Test: `tests/test_pipeline_nodes.py`

- [ ] **Step 1: 失败测试**——含 ` ```quiz\n<QuizBlock>...\n``` ` 的 mdx 应被 check 报 `ILLEGAL_CODE_FENCE`（owner 指向该文件、可 repair）。
- [ ] **Step 2 检测侧:** check 扫每个 ` ```<lang> ` 围栏，`lang` 不在白名单 `{mermaid, math, ...}` 且围栏体内含 `<Quiz`/`<BookFigure` 等组件标签时，报 `ILLEGAL_CODE_FENCE`；repair 走 mdx 原地编辑（删围栏）。
- [ ] **Step 3 生成侧:** 在 section_agent quiz 段加一句硬约束：「`<QuizBlock>`/`<QuizItem>`/`<QuizItemSlot>` 必须裸写为 MDX，**绝不可**用 ` ``` ` 代码围栏（含 ` ```quiz `）包裹；代码围栏仅用于 ` ```mermaid `。」
- [ ] **Step 4:** 跑测试 PASS，commit `fix(quiz): 禁止 QuizBlock 进代码围栏 + check 加围栏白名单拦截`。

---

## 待决（动手前需用户确认）

1. **build 是否进 check loop（Task 6 可选扩展）:** 只跑 `types:check`（快、抓不到 Shiki 类运行时错误），还是追加 `pnpm build`（慢一轮、能抓运行时渲染错误）？倾向：Task B 的白名单检测先兜已知围栏问题；完整 build 作为可配置开关（`generation.siteBuildCheck`）默认 off、需要时 on。
2. **A/B 与 site 重构的先后:** Task A/B 与 site 重构正交，建议先合 A/B（直接缓解 exhausted 与渲染报错），再做 site 重构大改。

---

## Self-Review

- **Spec 覆盖:** site 单一来源（Task 4/5）、归一化前移（Task 2）、materialize 拆分/瘦身（Task 3/7）、保留依赖让 build 便宜（Task 1）、check 用真 site（Task 6）、owner_task_id（A）、` ```quiz `（B）——均有对应 Task。
- **类型一致:** 全程函数名统一 `scaffold_site_template`（Task 3 定义、5/6/7 消费），别名 `materialize_site` 仅 Task 3→7 过渡期存在、Task 7 删除。
- **占位扫描:** 无 TODO/TBD；路径替换类给了 old→new 明确值；测试给了断言代码。
