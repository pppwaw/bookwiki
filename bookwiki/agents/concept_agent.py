from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.common import Citation
from bookwiki.schemas.concept import ConceptResult


class ConceptAgent:
    kind: ClassVar[str] = "concept_llm_v1"
    output_model: ClassVar[type[ConceptResult]] = ConceptResult
    model_key: ClassVar[str] = "concept"
    prompt_name: ClassVar[str] = "concept"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""You are the concept-page agent.

Write a concise concept page suitable for a Fumadocs MDX learning site.
Explain the concept, why it matters, and how it relates to linked chapters.
Use related only for closely connected concepts that are supported by input.
Keep citations grounded in available chapter/source context.
Use Markdown math syntax: $...$ for inline formulas and $$...$$ for display formulas.
Do not use \\( ... \\) or \\[ ... \\] math delimiters.
Do not invent cross-links or facts.""",
    )

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> ConceptResult:
        name = str(inp.get("canonical") or inp.get("name") or "Concept")
        chapters = [str(ch) for ch in inp.get("source_chapter_ids", ["ch01"])]
        contexts = [item for item in inp.get("chapter_contexts", []) if isinstance(item, dict)]
        citations = _context_citations(contexts)
        draft = ConceptResult(
            name=name,
            body_md=_draft_body(name, chapters, contexts),
            related=[],
            citations=citations,
            owner_task_id=f"concept:{name}",
        )
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=ConceptResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
            allowed_citation_refs={item.ref_id for item in citations},
        )
        return ConceptResult.model_validate(result)


def _draft_body(name: str, chapters: list[str], contexts: list[dict[str, Any]]) -> str:
    if not contexts:
        return f"{name} is a reconciled concept linked from {', '.join(chapters)}."
    chapter_titles = ", ".join(
        str(item.get("title") or item.get("chapter_id")) for item in contexts
    )
    return f"{name} is a reconciled concept linked from {chapter_titles}."


def _context_citations(contexts: list[dict[str, Any]]) -> list[Citation]:
    for context in contexts:
        for item in context.get("citations", []):
            ref_id = str(item.get("ref_id", "")).strip()
            quote = str(item.get("quote", "")).strip()
            if ref_id and quote:
                return [Citation(ref_id=ref_id, quote=quote)]
        for ref_id in _source_refs(str(context.get("source_md", ""))):
            return [Citation(ref_id=ref_id, quote="source context")]
    return []


def _source_refs(source_md: str) -> list[str]:
    import re

    return re.findall(r"source_ref:\s*([^\s>]+)", source_md)
