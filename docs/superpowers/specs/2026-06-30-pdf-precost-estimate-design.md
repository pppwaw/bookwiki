# PDF 预估价(Pre-convert Cost Estimate)设计

日期:2026-06-30
状态:待评审

## 背景与问题

`--dry-run` 当前只能在 `structure` 阶段跑完、产出 `work/structure/*.yaml` 之后,才能数出真实节数;在那之前 `_estimate_chapter_count`(`bookwiki/scheduler/lg_runner.py`)硬编码回退为 **2 章**,给出的估价几乎无参考价值。

但用户最需要估价的时机,恰恰是**刚把一批 PDF 放进 `books/<book>/input/`、还没花钱跑 convert 的时候**——他要据此判断这本书值不值得跑(实测一本 calculus「理想一次过」约 ¥70)。

目标:让 `--dry-run` 在 convert 之前、仅凭 `input/` 里的 PDF 就给出贴近实测的**单点「理想一次过」估价**,不依赖任何 LLM 调用。

## 关键事实(来自 calculus + circuits 双样本回测)

输入是**混合格式**:`input/` 下可同时有 PDF、PPTX、PPT、纯文本(`convert` 都支持)。calculus = 39 pdf + 1 ppt,circuits = 15 pdf + 1 pptx,ai = 1 pptx。

**回测只对比 calculus 与 circuits**(两本规模相近的正式教材);ai 只是一份复习纲要(9 节 / ¥4.5),样本不可比,排除。

被回测**证伪**的早期假设:
- ❌「文件数 ≈ 节数」:只 calculus 成立。circuits 15 PDF → 30 节(1:2)。**文件数不是节数信号**。
- ❌「concepts = 节数 × 3」:concept/节 calculus 3.0、circuits 4.0,随书变。
- ⚠️「caption 的图数口径」修正(不是证伪):早前误用 `assets/*.png`(含被跳过的公式图)当分母。改用**真正过 caption 的图数**(circuits 242 个 `vision_caption_llm_v2` 缓存)后,每图 ¥0.00026 vs calculus token 反推 ¥0.00028 ——**caption 确实 per-image 线性**(差 7%)。

被回测**确认稳定**(calculus vs circuits 误差 < 5%)的锚点:
- ✅ **PDF 页数 / 节数 ≈ 22**(21.62 vs 22.07,差 2%)。**页数才是节数信号**。
- ✅ **每节 generate 成本 ≈ ¥0.88**(0.898 vs 0.876,差 2.5%)。
- ✅ **理想一次过总成本 ≈ ¥70**(70.9 vs 67.9,差 4%);成本几乎全在 `generate` + `concept_pages`。

PPTX 用幻灯片数,密度与 PDF 页差一个数量级(ai ≈ 3.2 slide/节),需**单独换算系数**,但只有 ai 一个 pptx 样本,低置信。

## 设计目标的非目标(YAGNI)

- **不**做区间估价,只给单点「理想一次过」(用户已确认)。
- **不**把 repair/反复重跑的波动算进去(理想一次过剔除 repair)。
- **不**解析正文语义,只取 PDF 页数、PPTX 幻灯片数等结构性计数。
- **不**改动真实运行时计费(`LiteLLMRuntime`)——那是另一条权威链路。

## 架构:两段式估价

dry-run 估价分两条数据来源,**成本模型共用同一套单位成本**:

1. **有 structure 真实产物** → 用真实节数(及 concept 数,若 `concept-graph.json` 已存在)。最准,等价于现有 `_estimate_chapter_count` 的升级。
2. **无 structure 产物**(刚放入 input) → 扫 `input/` 所有格式,读 PDF 页数 + PPTX 幻灯片数 + PDF 内嵌图数,推出 sections / concepts / captioned_images。

无论走哪条,都把 `(sections, concepts, captioned_images)` 这一组**规模数**喂给同一个成本模型算钱。这样刚放输入能估、跑过 structure 后更准,平滑过渡。

### 模块边界(三层,各自可独立测试)

- **`bookwiki/scheduler/pdf_estimate.py`**(新)——纯输入扫描。
  - 输入:`input/` 目录路径。
  - 输出:`InputScan(pdf_pages: int, pptx_slides: int, pdf_images: int, unreadable: list[str])`。
  - 依赖:`pypdf`(PDF 页数 + 内嵌图)+ 标准库 `zipfile`(PPTX)。不碰成本。
- **`bookwiki/scheduler/dry_run.py`**(改)——纯成本模型,无 IO。
  - 输入:规模数 `sections / concepts / captioned_images`(以及一次性节点)。
  - 输出:`Estimate(tokens, cost_cny)`。
  - 把当前「按图节点」的 `ESTIMATE` 细化为**每节 / 每 concept / 每图 驱动 + 一次性**(见下)。
- **orchestrator**(在 `lg_runner.py` / `resume.py` 的 dry-run 路径)——决定走真实数据还是 PDF 扫描,组装规模数,调成本模型,格式化报告。

## 成本模型(细化后的 `dry_run.py`）

把单位成本按**驱动维度**拆开。理想一次过口径,标定自 run5(生成)+ run0(caption/structure)+ run9(index):

| 项 | 驱动 | 单位成本(¥) | 单位 tokens | 来源 |
|---|---|---|---|---|
| `generate` | 每节 | 0.898002 | 371_744 | run5 35.920 / 40 |
| `build_skeleton` | 每节 | 0.030981 | 29_299 | run5 1.239 / 40 |
| `concept_pages` | 每 concept | 0.254658 | 82_808 | run5 30.559 / 120 |
| `caption` | 每图片 | 0.00026 | 169 | circuits 242 图实测(当前 v2 口径)|
| `structure` | 一次性 | 1.026168 | 909_093 | run0 |
| `split` | 一次性 | 0.044038 | 42_234 | run5 |
| `index` | 一次性 | 0.054848 | 806_592 | run9 |
| `repair` | — | 0 | 0 | 理想一次过剔除 |
| `convert` / `reconcile_concepts` / `integrate` / `check` | — | 0 | 0 | 非 LLM / 自身不花钱 |

> **caption 按图片数估**(per-image,双样本确认):用真正过 caption 的图数(circuits 242 个 `vision_caption_llm_v2` 缓存)标定,每图 ¥0.00026 / 169 tok;calculus token 反推每图 ¥0.00028,差 7%。当前 v2 配置下 caption 极便宜(circuits 整本 ¥0.063,占总额 0.1%);calculus 旧值 ¥2.01 来自更贵的旧 vision 配置,不用作标定。
>
> **dry-run 的图数信号**:convert 前只有 PDF 内嵌图数(pypdf,circuits 2126),≠ 最终 captioned 图数(242,MinerU 筛掉公式/装饰图)。用 `captioned ≈ pdf_embedded × CAPTION_KEEP_RATIO`(circuits 242/2126 ≈ 0.11,单样本弱锚)。因 caption 占总额 < 0.5%,此处偏差对总估价无关紧要。

成本公式(理想一次过):

```
cost = sections   * (per_section_generate + per_section_skeleton)
     + concepts   *  per_concept
     + captioned  *  per_image_caption          # captioned ≈ pdf_embedded * CAPTION_KEEP_RATIO
     + once_structure + once_split + once_index
```

calculus 自洽校验:`40*(0.898002+0.030981) + 120*0.254658 + 2.0(旧 caption)+1.026168+0.044038+0.054848 ≈ ¥70.8` ✓
circuits 交叉校验:`30*0.928983 + 120*0.254658 + 0.063 + 1.0+0.044+0.055 ≈ ¥58.1`(实测理想 ¥67.9;差值主要来自 circuits 每 concept 略贵 ¥0.339 vs 标定 ¥0.255 —— 弱锚波动)。

## 规模推导(无 structure 时,扫 input)

扫 `input/` 下**所有**输入文件(不止 PDF),按格式取内容量:
- PDF → 页数(pypdf)
- PPTX → 幻灯片数(pptx 即 zip,数 `ppt/slides/slide*.xml`)
- PPT / 纯文本 → 字节数粗略折页(样本不足,低置信)

```
pdf_pages, pptx_slides, pdf_images = scan(input/*)
sections         = round(pdf_pages  / PAGES_PER_SECTION_PDF      # ≈ 22   (calculus 21.6 / circuits 22.1)
                       + pptx_slides / SLIDES_PER_SECTION_PPTX)  # ≈ 3.5  (仅 ai 单样本,低置信)
concepts         = round(sections   * CONCEPTS_PER_SECTION)      # ≈ 3.5  (calculus 3.0 ~ circuits 4.0 的中值)
captioned_images = round(pdf_images * CAPTION_KEEP_RATIO)        # ≈ 0.11 (circuits 242/2126,单样本;占总额 <0.5%)
```

- **文件数不作节数信号**(circuits 1 文件 = 2 节,已证伪)。
- `PAGES_PER_SECTION_PDF = 22` 是双样本确认的强锚(误差 2%)。`SLIDES_PER_SECTION_PPTX` 与 `CONCEPTS_PER_SECTION` 是弱锚,标注低置信,后续样本回归修正。

有 structure 时:`sections` 用真实节数;若 `work/concept-graph.json` 存在,`concepts` 用其真实节点数,否则 `sections * CONCEPTS_PER_SECTION`。

## 集成点

- `bookwiki/scheduler/lg_runner.py`:`_estimate_chapter_count` 升级为 orchestrator —— 先试 structure 真实数据,否则调 `pdf_estimate.scan` + 规模推导。`run_pipeline` 的 dry-run 分支把规模数传给成本模型。
- `bookwiki/scheduler/resume.py`:`dry_run_report` 接收规模数,报告里增加一行「估价依据」(真实 structure / PDF 扫描:N 文件、N 页、N 图)。
- `pyproject.toml`:`pypdf` 从 `mineru` extra 提升为**核心依赖**(用户偏好 pypdf;它本就是 mineru 传递依赖,提升无新风险)。

## 错误处理

估价是 best-effort 的离线工具,**任何单点失败都不应让 `--dry-run` 崩**,但也**不静默掩盖**(遵循 no-fallback:degrade 必须可见):

- 单个 PDF 读失败(加密/损坏)→ 记一条 `WARNING`(文件名 + 原因),该文件按**平均页数**计入,继续扫其余。报告末尾汇总「N 个 PDF 无法读取,已按均值估算」。
- `input/` 为空或不存在 → 报告明确说明「无输入 PDF,无法估价」,而非给 ¥0。
- `pypdf` import 失败(理论上不会,已是核心依赖)→ 报明确错误提示安装 `pypdf`,不静默给假数(无可靠的备选节数信号——文件数已证伪)。

## 测试

- `pdf_estimate.scan`:最小合成 PDF 固件(已知页数 + 内嵌图)+ 最小 PPTX 固件(已知幻灯片数)断言计数;损坏文件断言计入 `unreadable` + 不抛。
- `dry_run` 成本模型:纯函数,断言 `cost(sections=40, concepts=120, captioned_images=0) ≈ ¥70.8`(±容差);每节 / 每 concept / 每图 维度单独缩放断言。
- 两段式 orchestrator:有/无 structure 两种输入,断言走对分支、规模数正确(含 circuits 式「文件数 ≠ 节数」用例:页数→节数)。
- e2e:`test_dry_run_prints_mermaid_and_estimate` 扩展为「无 structure 时也能从 input PDF 给出非 fallback 估价」。

## 校准常数集中点

所有标定常数集中在 `dry_run.py` / `pdf_estimate.py` 顶部,带注释标明来源与置信度:

- **强锚(calculus + circuits 双样本,误差 < 8%)**:`PAGES_PER_SECTION_PDF ≈ 22`、`per_section_generate ≈ 0.88`、`per_image_caption ≈ ¥0.00026`、理想一次过总额 ≈ ¥70。
- **弱锚(跨书波动或单样本,标注低置信)**:`CONCEPTS_PER_SECTION`(3.0~4.0,取中值 3.5)、`per_concept`(0.25~0.34)、`SLIDES_PER_SECTION_PPTX`(仅 ai 一个 pptx 样本)、`CAPTION_KEEP_RATIO`(circuits 单样本 ≈ 0.11)。

后续每多跑一本书,把其 `run-manifest.json` + input 计数补进回测,回归修正弱锚。回测脚本逻辑应保留(可固化为 `scripts/` 下的一次性分析或测试),便于复跑。
