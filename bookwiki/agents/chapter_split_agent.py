from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.source import ChapterSplitResult
from bookwiki.split.chapter_splitter import split_sources_by_structure


class ChapterSplitAgent:
    kind: ClassVar[str] = "chapter_split_llm_v1"
    output_model: ClassVar[type[ChapterSplitResult]] = ChapterSplitResult
    model_key: ClassVar[str] = "split"
    prompt_name: ClassVar[str] = "chapter_split"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        body="""You are the chapter-split audit agent.

Review the deterministic source split for coverage and obvious assignment mistakes.
Preserve chapters, chapter_titles, alignment, and coverage exactly unless the input
explicitly asks you to repair them.
Write report_md as a concise audit note explaining source coverage, unassigned fragments,
and any risk.
Never move source text between chapters in this audit response.""",
    )

    async def run(
        self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime
    ) -> ChapterSplitResult:
        result = split_sources_by_structure(
            list(inp.get("source_paths", [])), str(inp.get("approved_structure", ""))
        )
        draft = ChapterSplitResult(
            chapters=result.chapters,
            chapter_titles=result.chapter_titles,
            alignment=result.alignment,
            coverage=result.coverage,
            report_md=result.report_md,
        )
        audit = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=ChapterSplitResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
        )
        audited = ChapterSplitResult.model_validate(audit)
        return ChapterSplitResult(
            chapters=draft.chapters,
            chapter_titles=draft.chapter_titles,
            alignment=draft.alignment,
            coverage=draft.coverage,
            report_md=audited.report_md or draft.report_md,
        )
