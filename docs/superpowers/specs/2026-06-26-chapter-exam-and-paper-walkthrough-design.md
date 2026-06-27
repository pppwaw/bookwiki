# 章末考试 + 原卷讲解 — 设计文档

- 日期:2026-06-26
- 状态:待评审(brainstorming 产出)
- 范围:生成管线(Python)+ 站点(Next.js/MDX)两个子系统

## 1. 背景与目标

当前项目已有较完整的 quiz 能力:
- **knowledge quiz**:SectionAgent 内联写进章节 MDX 的 `<QuizBlock>`(选择题,逐题即时反馈)。
- **application quiz**:`<QuizItemSlot/>` 由 `ApplicationQuizAgent` 填充;大题由 `WorkedApplicationQuizAgent` 产出 `WorkedItem`(`reference_answer` + 带 `weight` 的 `rubric`)。
- **判题**:`site-template/app/api/evaluate/route.ts` 已实现,按 rubric 逐条判分,返回 `score/verdict/feedback/revised_answer`。
- **索引**:`indexer/mdx_parser.py` 从 MDX 解析 `<QuizItem>`/`<WorkedProblem>` 进 `quiz_items` 表(已有 `type`/`options_json`/`grading_json` 列)。
- **站点**:`QuizBlock.tsx`(即时反馈)、`WorkedProblem.tsx`(大题:看参考答案 + 可选自测调 `/api/evaluate`)。

历年考卷目前只是丢进 `books/<book>/input/` 的 PDF,由 `structure` 阶段当作普通 source 映射成一个独立章节(如 `Mid-Term-Exam-for-Calculus.mdx`),既没参与各章出题,也没有针对性讲解。

本设计新增**两个协同特性**,共用一套"考卷判别 + 拆题"前置:

1. **章末考试(出题)**:把历年真题打散作为出题参考,为**普通内容章**生成一套混合题型的章末考试卷。
2. **原卷讲解**:对放进来的**考卷本身**逐题出讲解(参考答案/解题过程 + 考点点评)。

### 非目标(YAGNI)

- 不做成绩持久化 / 历史记录 / 排行(全程 in-session)。
- 不做服务端答案保密(威胁模型见 §2)。
- 不做 sqlite 动态组卷、不新增考试 API 路由、不改 `lib/sqlite.ts`(考试页直渲 MDX,见 §6)。
- 不做整卷"考点概览"页(已与用户确认砍掉)。

## 2. 威胁模型

**假设拥有代码/站点的是想复习的学生,不是想作弊的学生。** 推论:

- 考试题、答案、rubric 都可以是 MDX 明文,无需藏在服务端。
- "作答中不揭晓答案"只是促成真实自测的 **UX 礼貌**,不是安全边界。
- 因此无需任何混淆 / 服务端答案存储,大幅简化架构。

## 3. 共用前置:考卷判别 + 拆题

### 3.1 判别(软信号,复用现有链路)

- 在 `source_summary` / `structure` 阶段给每个 source 增加 `is_exam: bool` 判别,**复用既有 LLM 调用,不新增独立调用**。
- `is_exam` 是**软信号**:只决定"是否进入拆题 + 真题池 + 标 `from_exam`",**不影响考试/讲解能否生成**。判错、漏判、整本书没真题,后续都能照常工作。
- 涉及 schema:`schemas/source.py`(`SourceSummaryResult` 增 `is_exam`、拆出的题项结构)。

### 3.2 拆题与映射

- 被判为考卷的 source → 拆成**单题**,每题按考点 / concept 命中**映射到相关的普通内容章**。
- 一道真题映射到一章;整卷自然跨多章。
- **映射不到任何章的真题不丢**:进 `_unmapped` 池,该书任意章出题时可低优先借鉴(全书综合留作后话)。
- 产物:每个普通章一份"关联真题池",落 `books/<book>/work/exam_pool/<chapter_id>.md`(或等价 JSON);整卷原题保留供特性 2。

### 3.3 章的两类归宿

| 章来源 | 特性 1(生成考试卷) | 特性 2(讲解) |
|---|---|---|
| 普通内容章 | ✅ 生成 `exam.mdx`,借鉴真题池 | — |
| 考卷 source(`is_exam`)成的章 | ❌(它本身就是卷) | ✅ 逐题讲解原卷 |

## 4. 共用基础设施:题型 schema

把题目重构为**判别联合**(`schemas/quiz.py`,`SCHEMA_VERSION` 递增):

```
ExamQuestion = single_choice | multiple_choice | fill_blank | worked
```

- `single_choice` / `multiple_choice`
  - `options: list[str]`、`answer: list[str]`(单选长度 1;多选可多),`explanation`
- `fill_blank`
  - `question`(占位符标空)、`accepted_answers: list[list[str]]`(每空一组可接受答案,归一化前)、`explanation`
- `worked`(**复用现有 `WorkedItem`,不引入 open_ended**)
  - `reference_answer: str`、`rubric: list[RubricPoint{point, weight}]`、`explanation`

共用字段:`id`、`question`、`concepts: list[str]`、`from_exam: bool`、`source_refs: list[str]`、`citations: list[Citation]`。

特性 1、特性 2 共用此联合。讲解里的"相关知识点回顾"作为题项上的一个可选字段(如 `concept_recap_md`)承载,供折叠展示。

> 重构会牵动 `checkers/`、`indexer/`、`integrator/`、`quiz_extractor.py`、站点组件全链路,需配套回归(见 §8)。`QuizItem` 旧形态向 `single_choice`/`multiple_choice` 迁移;`WorkedItem` 平移为 `worked`。

## 5. 特性 1:章末考试(出题)

### 5.1 出题 agent

- 新 `agents/exam_agent.py`(`ExamAgent`)。
- 输入:章节正文 + 章节结果 + 本章关联真题池(可空)。
- 输出:一套混合题型 `ExamResult`(judged union 列表)。
  - 有真题 → 借鉴其**套路 / 难度 / 题型配比**,改写对齐,命中标 `from_exam=true`。
  - **无真题 → 照常用章节正文出整卷**,功能不打折。
  - `worked` 题必须产出 `reference_answer` + `rubric`,否则判题无依据。
- 数量 / 配比由出题 prompt 控制(参考 `book.config.json` 的 `generation` 配置风格)。

### 5.2 校验

- `checkers/exam_checker.py`,按 type 分派,任一不满足报 issue,`owner_task_id = chXX:exam`:
  - choice:`answer` 非空且全部 ∈ `options`
  - fill_blank:`accepted_answers` 每空 ≥1 候选
  - worked:`reference_answer` 非空且 `rubric` ≥1 条
  - 全部:`source_refs` 真实存在

### 5.3 整合到"章内单独一页"

- 考试题**明文**写进 `<chapter>/exam.mdx`(章节目录组里的一页),作为该章最后一页。
- **单文件章提升为目录组**:原 `Chapter-X.mdx` 正文迁为 `<chapter>/index.mdx`,新增 `<chapter>/exam.mdx`,写 `<chapter>/meta.json`(`pages: [..., "exam"]`)。
- 已是目录组的章:加 `exam.mdx`,追加进 `leaf_ids`(`pipeline/nodes.py:3651` 写 group meta.json 处)。
- 涉及:`integrator/`、`pipeline/nodes.py`。

### 5.4 索引(副产品)

- `indexer/mdx_parser.py` / `quiz_extractor.py` 识别 `exam.mdx` 的新考试组件 + 新题型 → 写 `quiz_items`(`type` + `grading_json` + `from_exam`)。
- 仅为搜索 / 统计的副产品;**站点不依赖 sqlite 供考试**(见 §6)。

### 5.5 站点呈现:`<ExamBlock>`(考试模式)

- 由 fumadocs **直接渲染** `exam.mdx`,题目从 MDX children 读取。
- 行为:整卷一次呈现 → 作答中不反馈、不揭晓 → 提交后统一判分:
  - 选择:本地判(单选相等;多选集合相等,或按配置部分给分)。
  - 填空:本地归一化(去空格、小写、全角半角)后比对 `accepted_answers`。
  - worked:复用 `/api/evaluate`(题干 + `reference_answer` + `rubric` + 用户答案),借鉴 `WorkedProblem.tsx`。
- 汇总总分 + 逐题反馈(正确 / 部分 / 错误、缺漏要点)。**不持久化**。
- 组件族:`<ExamBlock>` + `<ExamChoice>` / `<ExamFillBlank>` / `<ExamWorked>`,与 `QuizBlock` 并存,共享 `MathText` / `KatexClient` / 选项渲染等原语。

## 6. 站点链路约定(重要)

考试页是 fumadocs **直渲 MDX** 的独立一页,因此:

- **不做 sqlite 动态组卷、不新增 API 路由、不改 `lib/sqlite.ts`。**
- 复用现有 `/api/evaluate` 判 worked。
- sqlite 里的 `quiz_items`(含 `from_exam`)退化为副产品(搜索 / 统计)。

## 7. 特性 2:原卷讲解

### 7.1 讲解 agent

- 新 `agents/exam_explain_agent.py`(`ExamExplainAgent`)。
- 输入:被判为考卷的原卷 source(整卷原题)。
- 输出:对**原卷逐题**的讲解(复用 §4 的判别联合,每题带:
  - 原题
  - `concept_recap_md`:相关知识点回顾(供折叠;**不剧透完整解法**)
  - 完整解题过程 / 参考答案 + 考点点评(大题带 rubric)

### 7.2 呈现结构(教学法:题在前)

每道题:

```
① 原题(可思考 / 作答)
② [可折叠] 相关知识点回顾   ← 卡住可展开,不剧透完整解法
③ 完整解题过程 + 考点点评
```

- 默认**逐题解析直接可见(只读)**;大题复用 `WorkedProblem.tsx`(本就支持"看参考答案" + "可选自测调 `/api/evaluate`")。
- 选择 / 填空:显示正确答案 + 解析。
- **不做整卷考点概览。**
- 理由:复习场景,题在前促成检索练习、贴近真实考试;知识点放在尝试之后落得更实。

### 7.3 整合

- 考卷章保留为它自己的一章 / 页,讲解逐题渲染进该章 MDX。
- `owner_task_id = chXX:explain`。

## 8. 错误处理与回退

- **无真题**:特性 1 照常出整卷(真题是可选增强)。
- **判别错 / 漏判**:`is_exam` 软信号,最坏情况退化为"普通章出普通考试卷",不致命。
- **映射不到章的真题**:进 `_unmapped` 池,不丢弃。
- **`/api/evaluate` 不可用**:worked 判分降级为"只展示参考答案 + rubric"(`WorkedProblem.tsx` 现有行为),选择 / 填空判分不受影响。
- **schema 迁移**:`SCHEMA_VERSION` 递增,旧产物按版本兼容读取。

## 9. 测试策略(对齐现有 `tests/` 回归风格)

- **schema**:判别联合 round-trip;每题型校验(多选集合、fill_blank 每空 ≥1 候选、worked 必带 `reference_answer`+`rubric`)。
- **checker**:每类缺字段报 issue;`source_refs` 真实存在;`owner_task_id` 正确。
- **拆题 / 映射**:考卷拆成单题并按考点落到正确章;无映射进 `_unmapped`;无真题时 ExamAgent 仍出整卷。
- **整合**:单文件章被提升为目录组 + `exam.mdx` 入 `meta.json`;考卷章生成逐题讲解。
- **索引**:`exam.mdx` 新组件被 extractor 解析进 `quiz_items`(`type` + `grading_json` + `from_exam`)。
- **站点**:`ExamBlock` 提交前不揭晓;提交后本地判选择 / 填空;worked 走 `/api/evaluate`;汇总总分;讲解页折叠知识点 + 默认显解析。

## 10. 受影响文件清单(概览)

生成端:
- `schemas/quiz.py`(判别联合)、`schemas/source.py`(`is_exam` + 拆题项)
- `agents/exam_agent.py`(新)、`agents/exam_explain_agent.py`(新)
- `agents/structure_agent.py` / `pipeline/structure_scan.py`(判别 + 拆题映射)
- `checkers/exam_checker.py`(新)、`checkers/quiz_extractor.py`(识别新组件)
- `integrator/`、`pipeline/nodes.py`(exam 页整合、单文件章提升目录组、meta.json)
- `indexer/mdx_parser.py`(解析新题型 / 组件)

站点端:
- `components/ExamBlock.tsx`(新)+ `ExamChoice` / `ExamFillBlank` / `ExamWorked`
- 复用 `components/WorkedProblem.tsx`、`app/api/evaluate/route.ts`

## 11. 待评审 / 开放点

- 出题数量与题型配比的默认值(沿用 `book.config.json` 的 `generation` 风格,具体数值实现时定)。
- 多选"部分给分"策略:默认集合全等才给分,是否需要部分分留作配置项。
