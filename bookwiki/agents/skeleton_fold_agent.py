from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.concepts import concept_key as _concept_key
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.skeleton import SkeletonFoldResult, SkeletonOp


class SkeletonFoldAgent:
    """Fold one chapter's concept candidates into the running skeleton registry.

    Input shape (built per chapter in ``build_skeleton_node``)::

        {
            "chapter_id": "ch03",
            "chapter_title": "神经网络",
            "chapter_order": 3,
            "candidates": [{"name": "反向传播", "source_refs": ["w3-p012"]}, ...],
            "registry": [{"canonical": "梯度下降", "aliases": ["gradient descent"]}, ...],
            "language": "zh-CN",
            "book_notes": "...",
        }

    Returns a :class:`SkeletonFoldResult` of *ops* (not a whole table). The model decides
    cross-language / synonym merges because it sees both the chapter's candidates and the
    full registry of already-known canonicals *in context* — no embeddings needed. A
    deterministic draft (used by ``TestLLMRuntime`` and as the model's starting point)
    adds every candidate that does not already resolve in the registry and lists the rest
    as ``uses``.
    """

    kind: ClassVar[str] = "skeleton_fold_llm_v1"
    output_model: ClassVar[type[SkeletonFoldResult]] = SkeletonFoldResult
    model_key: ClassVar[str] = "skeleton"
    prompt_name: ClassVar[str] = "skeleton_fold"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你是 BookWiki 的流式概念归并（skeleton fold）agent。

你会按章节顺序处理整本书。本次输入是**当前这一章**抽出的概念候选 `candidates`，以及
**到目前为止**已经登记的全书概念表 `registry`（仅含 canonical 名与别名）。

请只输出对登记表的**操作（ops）**，不要重复整张表。可用操作：
- `add_concept`：候选是一个**全新**概念（registry 里没有，且不是已有概念的别名/同义词/
  另一种语言写法）。给出 canonical 名，必要时附 aliases。
- `add_alias`：候选其实是某个**已有** canonical 的新写法/缩写 → 把它挂为该 canonical 的别名。
- `rename_canonical`：本章让你确信某个已有 canonical 应改用更规范的名字。
- `merge`：两个**已有**条目其实是同一个概念（尤其跨语言同义，如 `反向传播` 与
  `backpropagation`，或缩写 `BP`）→ 用 winner 吸收 loser。
- `split`：之前某次把两个不同概念错误合并了，本章看清后把它拆回多个 canonical。

并在 `uses` 中列出本章**引用了但并非首次引入**的已有 canonical（用于后续按章切片术语）。

要求：
- 跨语言/同义判断要靠 registry 上下文，宁可用 add_alias/merge 合并，也不要为同一概念
  造出两个 canonical（否则后续会生成重复的概念页）。
- 不要发明候选里不存在的概念；不要改动与本章无关的条目。
- 首次引入章节由系统按处理顺序确定，你无需输出 first_chapter。""",
    )

    async def run(
        self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime
    ) -> SkeletonFoldResult:
        draft = _draft_fold(inp)
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=SkeletonFoldResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
        )
        return SkeletonFoldResult.model_validate(result)


def _draft_fold(inp: dict[str, Any]) -> SkeletonFoldResult:
    registry = inp.get("registry", []) if isinstance(inp, dict) else []
    candidates = inp.get("candidates", []) if isinstance(inp, dict) else []

    known: set[str] = set()
    for entry in registry:
        if not isinstance(entry, dict):
            continue
        for variant in (entry.get("canonical"), *(entry.get("aliases") or [])):
            key = _concept_key(str(variant or ""))
            if key:
                known.add(key)

    ops: list[SkeletonOp] = []
    uses: list[str] = []
    seen_new: set[str] = set()
    for cand in candidates:
        name = str((cand.get("name") if isinstance(cand, dict) else cand) or "").strip()
        key = _concept_key(name)
        if not key:
            continue
        if key in known:
            canonical = _canonical_for(registry, key)
            if canonical and canonical not in uses:
                uses.append(canonical)
        elif key not in seen_new:
            seen_new.add(key)
            ops.append(SkeletonOp(op="add_concept", canonical=name))
    return SkeletonFoldResult(ops=ops, uses=uses)


def _canonical_for(registry: list[Any], key: str) -> str | None:
    for entry in registry:
        if not isinstance(entry, dict):
            continue
        canonical = str(entry.get("canonical") or "")
        for variant in (canonical, *(entry.get("aliases") or [])):
            if _concept_key(str(variant or "")) == key:
                return canonical
    return None
