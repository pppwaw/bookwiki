from __future__ import annotations

import re
from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.chunking import find_headings
from bookwiki.concepts import concept_key as _concept_key
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.skeleton import SkeletonExtractResult
from bookwiki.schemas.source import ConceptCandidate


class SkeletonExtractAgent:
    """Pass 1 of build_skeleton: pull concept candidates from one chapter source chunk.

    Runs once per source chunk (chapters are chunked by ``chunk_by_heading`` first), so a
    single call never sees more than one bounded slice — the per-chapter parallel pass
    that replaces the old whole-book ``SkeletonAgent`` call. Output is recall-oriented
    (names + the refs they appear in); the serial ``SkeletonFoldAgent`` does the
    cross-language / synonym merging afterwards.

    A deterministic draft (curated ``topics`` + real headings in the chunk) gives
    ``TestLLMRuntime`` an offline result and the real model a starting point to enrich
    from the chunk body.
    """

    kind: ClassVar[str] = "skeleton_extract_llm_v1"
    output_model: ClassVar[type[SkeletonExtractResult]] = SkeletonExtractResult
    model_key: ClassVar[str] = "skeleton"
    prompt_name: ClassVar[str] = "skeleton_extract"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""你是 BookWiki 的概念抽取（skeleton extract）agent。

你会拿到**一章中的一段**源 markdown（`source_md`）、该章的 `title` 与 curated `topics`。
请列出这一段里**确实出现**的、有教学价值的概念候选，放进 `candidates`，每项：
- `name`：概念名（按源文本里的写法）。
- `source_refs`：该概念出现处所在的 source_ref（从本段的 `source_refs` 里选，可为空）。

要求：
- 以**召回**为先：curated topics 漏掉但正文里出现的重要概念都要补上。
- 只抽名字，不要判断同义/跨语言归并（后续的归并步骤会做）。
- 不要发明源文本里不存在的概念，不要把管理性噪音/OCR 伪影当概念。""",
    )

    async def run(
        self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime
    ) -> SkeletonExtractResult:
        draft = _draft_extract(inp)
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=SkeletonExtractResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
        )
        return SkeletonExtractResult.model_validate(result)


def _draft_extract(inp: dict[str, Any]) -> SkeletonExtractResult:
    topics = inp.get("topics", []) if isinstance(inp, dict) else []
    source_md = str(inp.get("source_md", "") if isinstance(inp, dict) else "")
    source_refs = list(inp.get("source_refs", []) if isinstance(inp, dict) else [])
    title = str(inp.get("title", "") if isinstance(inp, dict) else "")

    names: list[str] = []
    seen: set[str] = set()

    def add(raw: str) -> None:
        name = _clean_term(raw)
        key = _concept_key(name)
        if not name or not key or key in seen:
            return
        # Skip the chapter's own wrapper title — it is not a concept.
        if title and _concept_key(name) == _concept_key(title):
            return
        seen.add(key)
        names.append(name)

    for topic in topics:
        add(str(topic))
    for _off, _level, heading in find_headings(source_md):
        add(heading)

    candidates = [ConceptCandidate(name=name, source_refs=list(source_refs)) for name in names]
    return SkeletonExtractResult(candidates=candidates)


def _clean_term(raw: str) -> str:
    term = re.sub(r"\([^)]*\)", "", raw)
    term = re.sub(r"^\s*\d+(\.\d+)*\s*", "", term)  # strip leading "3.2 " section numbers
    return re.sub(r"\s+", " ", term).strip(" -:：")
