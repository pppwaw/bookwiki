# 边界页跨章共享 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让一页同时含两章内容的"边界页"被相邻两章共享,使下一章拿回它第一页顶部的内容并能合法引用该页。

**Architecture:** 纯 split 阶段、确定性、单文件改动。在 `split_sources_by_structure` 遍历 fragment 时,检测 body 里"章节标题且标题前有正文"的边界 fragment,把它额外补给阅读顺序的后继叶子章(reason=`boundary_carry`);coverage 改为按唯一 fragment 计数。不动 structure/schema/LLM prompt。

**Tech Stack:** Python 3.12,pytest,`uv`。改动集中在 `bookwiki/split/chapter_splitter.py`,测试在 `tests/test_m3_structure_split.py`。

**Spec:** `docs/superpowers/specs/2026-07-01-boundary-page-multi-chapter-design.md`

## Global Constraints

- **整页共享语义(approach B)**:边界 fragment 整块复制进两章,不做页内切分。
- **检测在 fragment 层**:判据 = body 含章节标题(`# Chapter N` / `# 第N章`)且标题前有非空白正文(实测验证,见 spec §7)。
- **后继用阅读顺序**,不用标题字符串反查 spec(`chapter_id` 是 title slug 且 title 可能被 LLM 改写)。
- **仅 `reason == "source_ref"`(confidence 1.0)命中才触发补给**;keyword 兜底不触发。
- **可审计**:补给记入 `alignment`,`reason="boundary_carry"`。
- **无静默截断**:检出边界却无后继章时 `_LOG.info` 记录,不报错、不吞掉。
- **不改** `bookwiki/pipeline/structure.py`、schema、任何 LLM prompt。
- 测试运行命令统一:`PYTHONPATH=. uv run --active pytest <target> -q`(worktree 内)。

---

### Task 1: 边界检测纯函数 `_is_boundary_fragment`

**Files:**
- Modify: `bookwiki/split/chapter_splitter.py`(新增 `import logging`、`_LOG`、`_CHAPTER_HEADING_RE`、`_is_boundary_fragment`,放在 `_assign_fragment` 之前,约 369 行前)
- Test: `tests/test_m3_structure_split.py`

**Interfaces:**
- Produces:
  - `_CHAPTER_HEADING_RE: re.Pattern` — 匹配 markdown 章节标题行(英 `# Chapter N` / 中 `# 第N章`)。
  - `_is_boundary_fragment(fragment: SourceFragment) -> bool` — body 含章节标题且标题前有非空白正文时返回 `True`。

- [ ] **Step 1: 写失败测试**

在 `tests/test_m3_structure_split.py` 末尾追加。先确认文件顶部已从 `bookwiki.split.chapter_splitter` 导入 `SourceFragment`;若无则在该 import 块补上 `SourceFragment`。

```python
from bookwiki.split.chapter_splitter import SourceFragment, _is_boundary_fragment


def _frag(body: str) -> SourceFragment:
    return SourceFragment(
        source_path="bk.md", source_id="bk", source_ref="bk-p020", body=body
    )


def test_is_boundary_fragment_detects_heading_with_leading_body() -> None:
    # 跨页:上一章结尾 + 新章标题(英/中)
    assert _is_boundary_fragment(
        _frag("Final paragraph of chapter five.\n\n# Chapter 6 Point Estimation\n\nOpening.")
    )
    assert _is_boundary_fragment(
        _frag("上一章结尾段落。\n\n# 第 6 章 点估计\n\n本章开头。")
    )


def test_is_boundary_fragment_ignores_clean_start_and_non_chapter() -> None:
    # 干净章起始:标题在开头,前面无正文
    assert not _is_boundary_fragment(_frag("# Chapter 6 Point Estimation\n\nFresh page top."))
    assert not _is_boundary_fragment(_frag("# 第6章 点估计\n\n本章开头。"))
    # 非章节标题 / 子节 / 纯正文
    assert not _is_boundary_fragment(_frag("## 6.3 Subsection\n\nnot a chapter heading"))
    assert not _is_boundary_fragment(_frag("Just prose mentioning chapter six, no heading."))
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONPATH=. uv run --active pytest tests/test_m3_structure_split.py::test_is_boundary_fragment_detects_heading_with_leading_body -q`
Expected: FAIL — `ImportError: cannot import name '_is_boundary_fragment'`。

- [ ] **Step 3: 最小实现**

在 `bookwiki/split/chapter_splitter.py` 顶部 import 区加入(与现有 `import re` 同块):

```python
import logging
```

在模块级(现有常量 `APPENDIX_CHAPTER_ID = "appendix"` 附近)加入:

```python
_LOG = logging.getLogger(__name__)

# A chapter-level markdown heading: English "# Chapter 6 ..." or Chinese "# 第6章 ...".
# Sub-section headings ("## 6.3 ...") and prose mentions are intentionally excluded.
_CHAPTER_HEADING_RE = re.compile(
    r"(?m)^#{1,6}[ \t]+(?:chapter[ \t]+\d+|第\s*[0-9〇一二三四五六七八九十百千]+\s*章)",
    re.IGNORECASE,
)
```

在 `_assign_fragment` 定义之前加入:

```python
def _is_boundary_fragment(fragment: SourceFragment) -> bool:
    """True when this page fragment straddles a chapter boundary.

    A boundary page carries the previous chapter's tail *and* the next chapter's
    opening: its body contains a chapter heading with non-whitespace text before it.
    A clean chapter start (heading at the top, nothing before) is not a boundary.
    """
    match = _CHAPTER_HEADING_RE.search(fragment.body)
    return bool(match) and bool(fragment.body[: match.start()].strip())
```

- [ ] **Step 4: 运行,确认通过**

Run: `PYTHONPATH=. uv run --active pytest tests/test_m3_structure_split.py -k is_boundary_fragment -q`
Expected: PASS(2 passed)。

- [ ] **Step 5: 提交**

```bash
git add bookwiki/split/chapter_splitter.py tests/test_m3_structure_split.py
git commit -m "feat(split): 新增边界页检测 _is_boundary_fragment"
```

---

### Task 2: 补给后继章 + coverage 按唯一 fragment 计数

**Files:**
- Modify: `bookwiki/split/chapter_splitter.py`(`split_sources_by_structure`,当前约 301–355:分配循环 317–330、coverage 337–344)
- Test: `tests/test_m3_structure_split.py`

**Interfaces:**
- Consumes: `_is_boundary_fragment`(Task 1)、`_assign_fragment`(现有,返回 `(chapter_id, confidence, reason)`,精确命中时 `reason == "source_ref"`)。
- Produces: `split_sources_by_structure` 对边界 fragment 额外产出一条 `alignment` 记录(`reason="boundary_carry"`),并把该 fragment 追加进后继叶子章的 `chapter_fragments` 桶;`coverage` 的 `total_fragments`/`assigned_fragments`/`assigned_ratio` 以唯一 `(source_path, source_ref)` 计。

- [ ] **Step 1: 写失败测试(正例 + 负例)**

追加到 `tests/test_m3_structure_split.py`:

```python
BOUNDARY_APPROVED = """chapters:
  - title: Chapter Five Foundations
    topics:
      - Foundations
    source_refs:
      - bk-p019
      - bk-p020
  - title: Chapter Six Point Estimation
    topics:
      - Point estimation
    source_refs:
      - bk-p021
"""


def test_split_shares_boundary_page_with_next_chapter(tmp_path: Path) -> None:
    source = tmp_path / "bk.md"
    source.write_text(
        "# Chapter 5 Foundations\n\n"
        "<!-- source_ref: bk-p019 -->\n\nEarlier chapter five content.\n\n"
        "<!-- source_ref: bk-p020 -->\n\n"
        "Final paragraph closing chapter five.\n\n"
        "# Chapter 6 Point Estimation\n\nOpening of chapter six on the same page.\n\n"
        "<!-- source_ref: bk-p021 -->\n\nChapter six on its own fresh page.\n",
        encoding="utf-8",
    )

    result = split_sources_by_structure([source], BOUNDARY_APPROVED)

    five = result.chapters["Chapter-Five-Foundations"]
    six = result.chapters["Chapter-Six-Point-Estimation"]
    # 边界页 p020 的正文同时进两章
    assert "Opening of chapter six on the same page." in five  # 原本就在 ch5(整页)
    assert "Opening of chapter six on the same page." in six   # 被补给 ch6
    assert "<!-- source_ref: bk-p020 -->" in six
    # 审计:一条 boundary_carry 记录
    assert any(
        item["source_ref"] == "bk-p020"
        and item["chapter_id"] == "Chapter-Six-Point-Estimation"
        and item["reason"] == "boundary_carry"
        for item in result.alignment
    )
    # coverage 不因重复分配而 > 1.0
    assert result.coverage["assigned_ratio"] <= 1.0


def test_split_clean_chapter_start_is_not_shared(tmp_path: Path) -> None:
    source = tmp_path / "bk.md"
    source.write_text(
        "# Chapter 5 Foundations\n\n"
        "<!-- source_ref: bk-p019 -->\n\nChapter five fills page nineteen.\n\n"
        "<!-- source_ref: bk-p020 -->\n\n"
        "# Chapter 6 Point Estimation\n\nChapter six starts at the top of a fresh page.\n\n"
        "<!-- source_ref: bk-p021 -->\n\nChapter six continues.\n",
        encoding="utf-8",
    )
    # 干净起始:p020 归 ch6 自己,不应产生 boundary_carry
    approved = """chapters:
  - title: Chapter Five Foundations
    topics:
      - Foundations
    source_refs:
      - bk-p019
  - title: Chapter Six Point Estimation
    topics:
      - Point estimation
    source_refs:
      - bk-p020
      - bk-p021
"""

    result = split_sources_by_structure([source], approved)

    assert not any(item["reason"] == "boundary_carry" for item in result.alignment)
    assert "bk-p020" not in result.chapters["Chapter-Five-Foundations"]
```

- [ ] **Step 2: 运行,确认失败**

Run: `PYTHONPATH=. uv run --active pytest tests/test_m3_structure_split.py::test_split_shares_boundary_page_with_next_chapter -q`
Expected: FAIL — `bk-p020` 不在 ch6(当前整页只判给 ch5),且无 `boundary_carry` 记录。

- [ ] **Step 3: 实现补给逻辑**

在 `split_sources_by_structure` 中,把当前分配循环(约 317–330):

```python
    for fragment in fragments:
        chapter_id, confidence, reason = _assign_fragment(fragment, specs)
        chapter_fragments.setdefault(chapter_id, []).append(fragment)
        alignment.append(
            {
                "source_path": fragment.source_path,
                "source_id": fragment.source_id,
                "source_ref": fragment.source_ref,
                "chapter_id": chapter_id,
                "confidence": confidence,
                "reason": reason,
                "chars": len(fragment.body),
            }
        )
```

替换为(在循环前建立后继表,循环内追加边界补给):

```python
    # Reading-order leaf successor: a boundary page whose body opens the next chapter
    # is carried into that successor so the new chapter recovers its first-page opening.
    leaf_order = [spec.chapter_id for spec in specs]
    successor = {
        cid: leaf_order[i + 1] for i, cid in enumerate(leaf_order) if i + 1 < len(leaf_order)
    }

    def _record(fragment: SourceFragment, chapter_id: str, confidence: float, reason: str) -> None:
        chapter_fragments.setdefault(chapter_id, []).append(fragment)
        alignment.append(
            {
                "source_path": fragment.source_path,
                "source_id": fragment.source_id,
                "source_ref": fragment.source_ref,
                "chapter_id": chapter_id,
                "confidence": confidence,
                "reason": reason,
                "chars": len(fragment.body),
            }
        )

    for fragment in fragments:
        chapter_id, confidence, reason = _assign_fragment(fragment, specs)
        _record(fragment, chapter_id, confidence, reason)
        # Boundary page: only when assigned by an explicit source_ref match (high confidence).
        if reason == "source_ref" and _is_boundary_fragment(fragment):
            next_id = successor.get(chapter_id)
            if next_id is None:
                _LOG.info(
                    "boundary fragment %s in last chapter %r has no successor to carry into",
                    fragment.source_ref,
                    chapter_id,
                )
            elif fragment not in chapter_fragments.get(next_id, []):
                _record(fragment, next_id, 1.0, "boundary_carry")
```

- [ ] **Step 4: 改 coverage 按唯一 fragment 计数**

把当前 coverage 块(约 337–344):

```python
    assigned_count = sum(1 for item in alignment if item["chapter_id"] != "appendix")
    total_count = len(alignment)
    coverage = {
        "total_fragments": total_count,
        "assigned_fragments": assigned_count,
        "unassigned_fragments": total_count - assigned_count,
        "assigned_ratio": round(assigned_count / total_count, 4) if total_count else 1.0,
    }
```

替换为:

```python
    # A boundary fragment maps to two chapters (two alignment rows); count coverage by
    # unique fragment so assigned_ratio stays a real fraction (never > 1.0).
    fragment_keys = {(item["source_path"], item["source_ref"]) for item in alignment}
    assigned_keys = {
        (item["source_path"], item["source_ref"])
        for item in alignment
        if item["chapter_id"] != APPENDIX_CHAPTER_ID
    }
    total_count = len(fragment_keys)
    assigned_count = len(assigned_keys)
    coverage = {
        "total_fragments": total_count,
        "assigned_fragments": assigned_count,
        "unassigned_fragments": total_count - assigned_count,
        "assigned_ratio": round(assigned_count / total_count, 4) if total_count else 1.0,
    }
```

- [ ] **Step 5: 运行,确认通过**

Run: `PYTHONPATH=. uv run --active pytest tests/test_m3_structure_split.py -k "boundary or clean_chapter_start" -q`
Expected: PASS(2 passed)。

- [ ] **Step 6: 提交**

```bash
git add bookwiki/split/chapter_splitter.py tests/test_m3_structure_split.py
git commit -m "feat(split): 边界页补给后继章 + coverage 按唯一 fragment 计数"
```

---

### Task 3: 边界情况 — 末章不补给、keyword 兜底不触发

**Files:**
- Test: `tests/test_m3_structure_split.py`(仅新增测试,验证 Task 2 已实现的守卫;无需改产品代码)

**Interfaces:**
- Consumes: Task 2 的 `split_sources_by_structure` 行为。

- [ ] **Step 1: 写测试(末章边界 + keyword)**

追加到 `tests/test_m3_structure_split.py`:

```python
def test_split_boundary_in_last_chapter_does_not_error(tmp_path: Path) -> None:
    # 章节标题出现在最后一个叶子章的页里 → 无后继,不补给、不报错
    source = tmp_path / "bk.md"
    source.write_text(
        "# Only Chapter\n\n"
        "<!-- source_ref: bk-p001 -->\n\nBody before a stray heading.\n\n"
        "# Chapter 9 Trailing\n\nContent after heading still in the only chapter.\n",
        encoding="utf-8",
    )
    approved = """chapters:
  - title: Only Chapter
    topics:
      - Whatever
    source_refs:
      - bk-p001
"""

    result = split_sources_by_structure([source], approved)

    assert not any(item["reason"] == "boundary_carry" for item in result.alignment)
    assert result.coverage["assigned_ratio"] <= 1.0


def test_split_keyword_fallback_fragment_is_not_carried(tmp_path: Path) -> None:
    # p777 未被任何章 source_ref 声明 → 走 keyword/appendix,即便 body 含章标题也不补给
    source = tmp_path / "bk.md"
    source.write_text(
        "# Book\n\n"
        "<!-- source_ref: bk-p001 -->\n\nFoundations content.\n\n"
        "<!-- source_ref: bk-p777 -->\n\n"
        "Loose page tail.\n\n# Chapter 6 Point Estimation\n\nHeading with no declaring chapter.\n",
        encoding="utf-8",
    )
    approved = """chapters:
  - title: Foundations
    topics:
      - Foundations
    source_refs:
      - bk-p001
  - title: Point Estimation
    topics:
      - Point estimation
    source_refs:
      - bk-p002
"""

    result = split_sources_by_structure([source], approved)

    # p777 不是任何章的显式 source_ref → 不应产生 boundary_carry
    assert not any(
        item["source_ref"] == "bk-p777" and item["reason"] == "boundary_carry"
        for item in result.alignment
    )
```

- [ ] **Step 2: 运行,确认通过**

Run: `PYTHONPATH=. uv run --active pytest tests/test_m3_structure_split.py -k "last_chapter or keyword_fallback" -q`
Expected: PASS(2 passed)。若 keyword 测试意外失败,说明 `_assign_fragment` 对 `bk-p777` 返回了 `reason == "source_ref"`——检查 fixture 确保 p777 不落在任何声明的 range 内。

- [ ] **Step 3: 提交**

```bash
git add tests/test_m3_structure_split.py
git commit -m "test(split): 覆盖末章边界与 keyword 兜底不触发补给"
```

---

### Task 4: 全量回归 + lint

**Files:** 无(验证任务)

- [ ] **Step 1: 跑整个 split/structure 测试文件**

Run: `PYTHONPATH=. uv run --active pytest tests/test_m3_structure_split.py -q`
Expected: 全部 PASS(含既有用例;既有 `assigned_ratio == 1.0` 的用例在无边界页时不受影响)。

- [ ] **Step 2: 跑 chunking 相关测试(确认未误伤)**

Run: `PYTHONPATH=. uv run --active pytest tests/test_chunking.py tests/test_structure_scan.py -q`
Expected: 全部 PASS。

- [ ] **Step 3: lint / 格式检查**

Run: `uv run --active ruff check bookwiki/split/chapter_splitter.py tests/test_m3_structure_split.py`
Expected: no errors(若项目用其他 linter,以 `pyproject.toml` 配置为准)。

- [ ] **Step 4: 若前面 lint 有修改则提交**

```bash
git add -A
git commit -m "chore(split): lint 修复" || echo "no lint changes"
```

---

## Self-Review

- **Spec coverage**:§3.1 检测 → Task 1;§3.2 补给后继章 → Task 2 Step 3;§3.3 coverage → Task 2 Step 4;§4 边界情况(末章/keyword/非章标题)→ Task 1 负例 + Task 3;§5 测试(正/负/末章/keyword/回归)→ Task 1–4。✅
- **Placeholder scan**:无 TBD/TODO;每个代码步都给出完整代码与命令。✅
- **Type consistency**:`_is_boundary_fragment(SourceFragment) -> bool` 在 Task 1 定义、Task 2 使用一致;`reason` 字符串 `"source_ref"` / `"boundary_carry"` 全程一致;coverage 键名与既有 `_render_report` 消费的 `total_fragments`/`assigned_fragments`/`unassigned_fragments`/`assigned_ratio` 一致。✅
- **假设**:边界页由 structure 声明给"结束的那一章"(上一章),故其后继叶子即新章;若某书结构把边界页声明给了新章本身,则该页已属新章、`_is_boundary_fragment` 仍为真但 `reason` 仍是 `source_ref`——会额外补给新章的后继(潜在误补)。此为低概率,`boundary_carry` 审计可发现;如需收紧,可在后续加"仅当 body 首个章标题对应的编号 > 当前章"之类校验(不在本次范围)。
