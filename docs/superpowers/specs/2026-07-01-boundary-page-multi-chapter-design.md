# 边界页跨章共享设计(Boundary-Page Multi-Chapter)

- 日期:2026-07-01
- 分支:`worktree-boundary-page-multi-chapter`
- 状态:待 review

## 1. 问题

一本书里,某一页(一个 `<!-- source_ref: pXX -->` 对应的物理页)可能同时包含**上一章的结尾**和**下一章的开头**——章节标题(如 `Chapter 6 …` / `第 6 章 …`)出现在页面中部。

当前 pipeline 把这样一页整个判给**一个**章,导致**下一章丢掉它第一页顶部的内容,并且永远无法引用(cite)这一页**。

### 承重假设:页标记只有起始、无结束

`SOURCE_REF_RE = <!--\s*source_ref:\s*([A-Za-z0-9_.-]+)\s*-->`(`convert/common.py:7`)是**单个自闭合起始标记**,**没有配对的结束标记**。一页的内容从它自己的标记开始、延续到**下一个**标记为止;`extract_source_fragments` 即 `text[match.end():next_start]`。

由此推出:边界页 ch6 那半页**没有属于自己的页标记**(它的起始标记 `p020` 已被 ch5 那半页"用掉"),所以边界页 fragment `p020` 的 body 里会夹着"ch5 尾 + `# Chapter 6` 标题 + ch6 头"。整页共享(approach B)也无需结束标记,直接整块复制即可。

### 根因定位

pipeline 有两套独立的切分,分别在两个阶段:

1. **structure(摘要)阶段** —— `pipeline/structure.py:228` 用 `chunk_by_heading()` **按标题切**(且只在超预算时才递归切)。逐页摘要 LLM 看不到"半页 ch5 + 半页 ch6";边界页 `p020` 的页标记物理上落进 ch5 那半页,故 `p020` 只被归给 ch5。

2. **split(分章)阶段** —— `split/chapter_splitter.py:266` 的 `extract_source_fragments()` **按页标记切**。边界页 fragment `p020 = [ch5 尾][# Chapter 6][ch6 开头]`(ch6 开头那半页没有属于自己的新页标记,下一个标记是 `p021`)。`_assign_fragment()`(`chapter_splitter.py:369-384`)按 `spec.source_refs` 精确/范围匹配,遇到第一个命中的章就 `return`,把整块判给 ch5。

**结论**:根因是 split 阶段"整页 fragment 只能归一个章"。修复也落在此:split 阶段的 fragment 本身就携带"标记与新章标题之间是否夹着上一章正文"这一决定性信息(见 §7 实测),故检测与补给都在此层完成。

## 2. 目标与非目标

**目标**:边界页 `p020` 同时归属相邻两章(ch5、ch6),两章都能拿到该页正文并合法 cite `p020`。采用**整页共享**语义(approach B):`p020` fragment 整块复制进两章;ch6 会顺带拿到 ch5 尾部内容作为噪音——这是整页共享的既定取舍,已确认接受。

**非目标**:
- 不做页内精确切分(approach A:在标题处把 fragment 切两半),复杂度过高。
- 不做 ±1 页无差别扩边(每章强行纳入前后邻页):会污染**所有**章节边界(含干净边界)的 `allowed_refs`,产生错误引用。
- 不引入 LLM 判定:检测走确定性 fragment 规则(实测验证)。
- 不改 `SourceSummaryResult` / `DetectedChapter` schema 结构,也不改任何 LLM prompt。

## 3. 方案:split fragment 层确定性检测

### 3.1 检测信号(fragment 层,实测验证)

检测落在 **split 阶段的 fragment 上**——fragment(`extract_source_fragments` 产物)= `[本页标记 → 下一页标记)` 的正文,正好是"一页的内容"。信号:

> **一个 fragment 的 body 里出现章节标题(`# Chapter N …` / `# 第 N 章 …`),且该标题之前还有非空白正文。**

- 跨页的 `p020` fragment = `"…ch5 尾…\n\n# Chapter 6 …\n\n…ch6 头…"` → 标题前有正文 → **触发**。
- 干净的章起始 `p020` fragment = `"# Chapter 6 …\n\n…"` → 标题在开头、前面无正文 → **不触发**。

实测确认(见 §7):**chunk 层无法区分这两者**(干净 / 跨页的章起始 chunk 都表现为"首标记前有内容"),只有 fragment 层的"标题前有正文"能精确区分。故检测**必须**落在 split fragment 层,不能放在 structure/chunk 层。

### 3.2 分配:补给阅读顺序后继章(`split/chapter_splitter.py`)

`split_sources_by_structure`(约 301–355)遍历 fragment 分章。改动:

- fragment `F` 经 `_assign_fragment` 以 **source_ref 精确命中**(confidence 1.0)判给章 `C` 后,若 `F` 命中 §3.1 边界信号,则把 `F` **也**分给阅读顺序里 `C` 的**后继叶子章** `C_next`(approved-structure 的 depth-first 叶子顺序,`C` 的下一个)。
- 用**阅读顺序后继**而非"标题字符串反查 spec":`chapter_id` 是 title 派生的 slug 且 title 可能被 LLM 改写,字符串匹配不可靠;而 specs 本就是阅读顺序,含新章标题的边界页,其后继叶子就是新章。
- 该补给记入 `alignment`,打 `reason="boundary_carry"`,体现在 `report_md`——**可审计**(审计点在 split 报告,而非 approved-structure YAML)。
- keyword 兜底命中(confidence < 1.0)**不**触发边界补给(低置信,避免误扩)。

结果:`p020` fragment(整块含 ch5 尾 + ch6 头)复制进 ch5、ch6 两章 `source.md`;ch6 = `[p020 fragment]` + `[p021 …]`,拿回自己第一页顶部的内容。ch6 顺带得到 ch5 尾部作为噪音——整页共享(approach B)的既定取舍。

### 3.3 coverage 统计

一个 fragment 现在可对应多个 chapter → coverage(`chapter_splitter.py:337-344`)的 `total`/`assigned` 按**唯一 fragment**(而非 alignment 条目数)计,否则 `assigned_ratio` 会 > 1.0。

### 3.4 下游安全性(已核验)

- `parse_approved_structure` 仅校验 `chapter_id` 唯一(`_assert_unique_ids`),**不禁止**跨章 source_ref 重叠。
- 全项目**无** `source_ref → chapter` 的全局反向索引;`alignment` 是 list,`chapter_source_refs` 是 `dict[str, list]`,均容忍重复 ref。
- `generate/sections.py:143` 的 `allowed_refs` 按每章 `source.md` 实际内容(`SOURCE_REF_RE.findall`)独立提取,ch6 拿到 `p020` fragment 后即可合法 cite `p020`,无需在结构 YAML 里声明。

## 4. 边界情况

- **标题不可正则识别**:检测复用 `_detect_all_chapter_headings` 的模式(`Chapter N` / `第N章`)。无编号的描述性标题(合并章 / 附录)检不出 → 该边界不补给。这类章通常本就不是页中边界;需要时可放宽到任意 H1。显式测试覆盖,不静默。
- **`C` 是最后一叶子章**(无后继):不补给(书末无下一章)。
- **fragment 经 keyword 兜底判入某章**(confidence < 1.0):不触发边界补给。
- **`C` 落 appendix / 未被任何章 source_ref 命中**:不补给(无明确前章归属)。
- **一个 fragment 含多个章标题**(极罕见):只补给紧邻的后继章一次;多重跨章不在本次范围,检出多标题则 `log`,不静默吞掉。
- **无静默截断**:检出边界信号却无法补给(如无后继章)的情况一律 `log`,不掩盖。

## 5. 测试

- **正例**:fixture 源含 `p020 = [ch5 尾][# Chapter 6 …][ch6 头]` 紧接 `p021`;approved-structure 里 ch5 声明 `p020`、ch6 声明 `p021`。断言:split 后 `p020` fragment body 同时出现在 ch5、ch6 两章 `source.md`;`alignment` 有 `p020 → ch6`、`reason="boundary_carry"`;`coverage["assigned_ratio"] ≤ 1.0`。
- **负例(防误判)**:干净章起始 fixture(`# Chapter 6` 紧跟 `p020` 标记、前无正文)断言 `p020` 只属其 source_ref 命中章,无 `boundary_carry`。
- **末章边界**:边界信号出现在最后一叶子章的 fragment → 不补给、有 `log`,不报错。
- **keyword 兜底**:confidence < 1.0 的 fragment 即使 body 含标题也不触发补给。
- **回归**:现有 structure/split 测试保持通过;默认无边界页时行为不变。

## 6. 改动清单

| 文件 | 改动 |
|------|------|
| `bookwiki/split/chapter_splitter.py` | `split_sources_by_structure` 内:检测 fragment body 章标题 + 前置正文;命中则把 fragment 补给阅读顺序后继叶子章,`reason="boundary_carry"`;coverage 按唯一 fragment 计数 |
| `tests/test_m3_structure_split.py` | 正例 / 负例 / 末章 / keyword / 回归 |

**单文件核心改动**(`chapter_splitter.py`),无 `structure.py` / schema / LLM prompt 变更。

## 7. 实测证据(为何检测必须在 fragment 层)

在真实 `chunk_by_heading` + `extract_source_fragments` 上跑了两个 fixture(跨页 / 干净章起始):

| 层级 | 跨页 `p020` | 干净章起始 `p020` | 能否区分 |
|------|------------|------------------|---------|
| **chunk 层**(`# Chapter 6` chunk:首标记前是否有内容 / `prev_last_ref`) | 有内容,`prev_last_ref=p020` | **同样**有内容,`prev_last_ref=p020` | ❌ 不能 |
| **fragment 层**(body 含章标题 + 标题前是否有正文) | `has_heading=True, content_before=True` | `has_heading=True, content_before=False` | ✅ 能 |

`chunk_by_heading` 仅在**超预算**时才按标题递归切;正常尺寸下,页标记(无论跨页共享还是干净页顶)都会落在"上一 chunk"里,导致章起始 chunk 在两种情形下形态一致 → chunk 层判据失效。fragment 层的 `[标记→标记)` 切片保留了"标记与标题之间是否夹着上一章正文"这一决定性信息,故检测锁定在此层。
