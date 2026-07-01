# 边界页跨章共享设计(Boundary-Page Multi-Chapter)

- 日期:2026-07-01
- 分支:`worktree-boundary-page-multi-chapter`
- 状态:待 review

## 1. 问题

一本书里,某一页(一个 `<!-- source_ref: pXX -->` 对应的物理页)可能同时包含**上一章的结尾**和**下一章的开头**——章节标题(如 `Chapter 6 …` / `第 6 章 …`)出现在页面中部。

当前 pipeline 把这样一页整个判给**一个**章,导致**下一章丢掉它第一页顶部的内容,并且永远无法引用(cite)这一页**。

### 承重假设:页标记只有起始、无结束

`SOURCE_REF_RE = <!--\s*source_ref:\s*([A-Za-z0-9_.-]+)\s*-->`(`convert/common.py:7`)是**单个自闭合起始标记**,**没有配对的结束标记**。一页的内容从它自己的标记开始、延续到**下一个**标记为止;`extract_source_fragments` 即 `text[match.end():next_start]`。

由此推出本设计成立的关键:边界页 ch6 那半页**没有属于自己的页标记**(它的起始标记 `p020` 已被 ch5 那半页"用掉"),所以 ch6 的 chunk 以"无标记内容"开头、直到下一个标记 `p021`——这正是 §3.1 检测信号能判定边界页的依据。整页共享(approach B)也无需结束标记,直接整块复制即可。

### 根因定位

pipeline 有两套独立的切分,分别在两个阶段:

1. **structure(摘要)阶段** —— `pipeline/structure.py:228` 用 `chunk_by_heading()` **按标题切**。章边界对齐标题,所以每次 `SourceSummaryAgent` 调用的 span 不会跨章;逐页摘要 LLM **看不到**"半页 ch5 + 半页 ch6"这个现象。边界页 `p20` 的页标记物理上位于 ch5 那半页顶部,落进 ch5 的 chunk,所以 `p20` 只被归给 ch5。

2. **split(分章)阶段** —— `split/chapter_splitter.py:266` 的 `extract_source_fragments()` **按页标记切**。边界页 fragment `p20 = [ch5 尾][# Chapter 6][ch6 开头]`(ch6 开头那半页没有属于自己的新页标记,下一个标记是 `p21`)。`_assign_fragment()`(`chapter_splitter.py:369-384`)按 `spec.source_refs` 精确/范围匹配,遇到第一个命中的章就 `return`,把整块判给 ch5。

**结论**:根因是 split 阶段"整页 fragment 只能归一个章",叠加 structure 阶段"边界页只声明给 ch5"。

## 2. 目标与非目标

**目标**:边界页 `p20` 同时归属相邻两章(ch5、ch6),两章都能拿到该页正文并合法 cite `p20`。采用**整页共享**语义(approach B):`p20` fragment 整块复制进两章;ch6 会顺带拿到 ch5 尾部内容作为噪音——这是整页共享的既定取舍,已确认接受。

**非目标**:
- 不做页内精确切分(approach A:在标题处把 fragment 切两半),复杂度过高。
- 不引入 LLM 判定(逐页 summary agent 结构上看不到跨章;另一个 LLM 判也只是二手信号且不稳定)。检测走确定性结构规则。
- 不改 `SourceSummaryResult` / `DetectedChapter` schema 结构(现有字段已足够)。

## 3. 方案:确定性结构检测 + split all-match

### 3.1 检测信号(精确、结构性)

边界信号只在 **chunk 原始文本仍在**的地方可见(`structure.py:233` 的 `c.text`;进入聚合层后只剩 source_refs 列表、marker 位置已丢失)。信号定义:

> **一个"章起始 chunk"(带 chapter 标题 / `detected_chapter_id`)的正文中,在它的第一个 `<!-- source_ref -->` 标记之前存在实质(非空白)内容。**

- 干净的章起始:页标记在页顶、位于标题之前 → 首标记在最前 → **不触发**。
- 跨页的章起始:标题在 `p20` 中部,ch6 半页无新页标记 → chunk 正文 = `# Chapter 6 …[正文]…<!-- source_ref: p21 -->`,标记前有内容 → **触发**。
- 触发时,**被共享的页 = 全局阅读顺序里上一个 chunk 的末个 `source_ref`**(即 ch5 的 `p20`)。

### 3.2 块①:检测 + 注入(`pipeline/structure.py`)

在 `structure_node` 的 chunk 循环(约 228–252)与 `_merge_source_chunk_summaries`(约 140–183)一带:

- 维护一个跨 chunk、跨源文件的 `last_ref`(全局阅读顺序中最近出现的 source_ref)。
- 对每个 chunk,用 `SOURCE_REF_RE.search(c.text)` 定位首个页标记位置;若该 chunk 是章起始(`c.detected_chapter_id` 或 `heading_path` 首段是 chapter 标题)且首标记前有非空白内容,则判定为边界页:把 `last_ref` 作为 `carried_ref` 记录。
- **注入点**:在拿到该 chunk 的 summary 结果后(合并前),把 `carried_ref` 并入该 chunk summary 的 `source_refs`,并并入其对应的 `detected_chapters[*].source_refs`(该章的那条)。这样:
  - `_merge_source_chunk_summaries` 的 `source_refs` 去重合并会带上 `carried_ref`;
  - `_chapter_specs_from_sources` / `_detected_chapter_items` 聚合出的**新章 plan.source_refs 就包含 `carried_ref`**;
  - `StructureAgent` draft 在 ch6 的 `source_refs` 里声明 `p20`;prompt 已要求"source_refs 与输入一致",LLM 会保留。
- 注入发生在 summary **之后**,不改 summary 的 LLM 输入/cache key,也不影响 coverage 审计(`covered_refs` 是 set,重复 ref 无害)。

全程 **source_ref 精确匹配**,不依赖任何标题字符串比对(`chapter_id` 是 title 派生的 slug 且 title 可能被 LLM 改写,字符串匹配不可靠)。

### 3.3 块②:split 改 all-match(`split/chapter_splitter.py`)

- `_assign_fragment`(369–384):由"返回第一个 source_ref 命中的章"改为"返回**所有** source_ref 显式命中的章"。**仅显式 source_ref 命中才多分配**;keyword 兜底路径仍单分配(避免把无标记片段全量复制)。签名从 `tuple[str, float, str]` 改为返回列表(如 `list[tuple[str, float, str]]`)。
- 分配循环(317–330):对返回的每个 chapter_id 都 `chapter_fragments[...].append(fragment)` 并追加一条 `alignment` 记录。
- coverage 统计(337–344):`total`/`assigned` 按**唯一 fragment**(而非 alignment 条目数)计,否则 `assigned_ratio` 会 > 1.0。

结果:结构里 `p20` 同时属 ch5、ch6 → fragment `p20` 复制进两章 `source.md`;ch6 = `[p20 fragment(含 ch5 尾 + ch6 头)]` + `[p21 …]`。

### 3.4 下游安全性(已核验)

- `parse_approved_structure` 仅校验 `chapter_id` 唯一(`_assert_unique_ids`),**不禁止**跨章 source_ref 重叠。
- 全项目**无** `source_ref → chapter` 的全局反向索引;`alignment` 是 list,`chapter_source_refs` 是 `dict[str, list]`,均容忍重复 ref。
- `generate/sections.py:143` 的 `allowed_refs` 按每章独立提取,天然容忍同一 ref 出现在多章。

## 4. 边界情况

- **首个 chunk**(无上一 chunk / `last_ref` 为空):不触发(书开头的章不跨页)。
- **上一 chunk 无 source_ref**:沿 `last_ref` 向前(它是全局滚动值,自然指向更早的 ref);仍为空则跳过并 `log`。
- **跨源文件边界**:`last_ref` 跨 `source_paths` 维护,指向全局阅读顺序上一个 ref。
- **一页含多个章标题**(极少见):按"首个标题触发一次共享"处理;更复杂的多重跨章不在本次范围,若检出多标题则 `log` 记录不静默吞掉。
- **无静默截断**:任何被跳过 / 无法解析的边界页都要 `log`,不掩盖。

## 5. 测试

- **正例**:fixture 源含 `p20 = [ch5 尾][# Chapter 6 …][ch6 头]` 紧接 `p21`。断言:
  - 结构:`p20 ∈ ch5.source_refs` 且 `p20 ∈ ch6.source_refs`;
  - split:`p20` fragment 同时出现在两章 `source.md`;
  - coverage:`assigned_ratio ≤ 1.0`。
- **负例(防误判)**:干净章起始 fixture(页标记在标题之前)断言**不触发**,`p_k` 只属新章。
- **split 单测**:同一 source_ref 显式命中两个 spec 时,`_assign_fragment` 返回两章;keyword 兜底片段仍单章。
- **回归**:现有 structure/split 测试(如 `.tmp/pytest-final-runtime-split/*` 覆盖的用例)保持通过;默认无边界页时行为不变。

## 6. 改动清单

| 文件 | 改动 |
|------|------|
| `bookwiki/pipeline/structure.py` | chunk 循环维护 `last_ref`;检测章起始 chunk 首标记前有内容;把 `carried_ref` 注入该 chunk summary 的 `source_refs` + 对应 `detected_chapters` |
| `bookwiki/split/chapter_splitter.py` | `_assign_fragment` 返回所有命中章;分配循环多章 append;coverage 按唯一 fragment 计数 |
| `tests/…` | 新增正例/负例/ split 单测;回归验证 |

无 schema 变更,无 LLM prompt 变更。
