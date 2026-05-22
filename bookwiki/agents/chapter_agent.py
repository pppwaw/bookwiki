from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import chapter_id, chapter_title, citation
from bookwiki.agents.llm import generate_with_llm
from bookwiki.agents.prompting import PromptTemplate
from bookwiki.scheduler.llm import LLMRuntime
from bookwiki.schemas.chapter import ChapterResult


class ChapterAgent:
    kind: ClassVar[str] = "chapter_llm_v1"
    output_model: ClassVar[type[ChapterResult]] = ChapterResult
    model_key: ClassVar[str] = "chapter"
    prompt_name: ClassVar[str] = "chapter"
    prompt_template: ClassVar[PromptTemplate] = PromptTemplate(
        version="v1",
        body="""You are the chapter authoring agent.

Write an Obsidian-ready chapter from the chapter source markdown.
Use clear section headings, concise explanations, and source-grounded examples.
Keep chapter_id, title, and owner_task_id stable.
Every citation must quote a short phrase that appears in the provided source.
Extract only concepts that are central to this chapter and useful for later concept pages.
Do not include unsupported facts, external knowledge, or generic filler.""",
    )

    async def run(self, inp: dict[str, Any], *, model: str, runtime: LLMRuntime) -> ChapterResult:
        ch_id = chapter_id(inp)
        title = chapter_title(inp)
        draft = ChapterResult(
            chapter_id=ch_id,
            title=title,
            body_md=(
                f"# {title}\n\n"
                f"Draft chapter generated from `{inp.get('source_path', 'source')}`. "
                "Rewrite it into study-ready prose grounded in the source."
            ),
            concepts=[f"{title} concept"],
            citations=[citation(inp)],
            owner_task_id=f"{ch_id}:chapter",
        )
        result = await generate_with_llm(
            runtime=runtime,
            model=model,
            output_model=ChapterResult,
            agent_name=self.__class__.__name__,
            prompt_name=self.prompt_name,
            prompt_template=self.prompt_template,
            inp=inp,
            draft=draft,
        )
        return ChapterResult.model_validate(result)
