from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import chapter_id, chapter_title, citation
from bookwiki.schemas.chapter import ChapterResult


class ChapterAgent:
    kind: ClassVar[str] = "chapter"
    output_model: ClassVar[type[ChapterResult]] = ChapterResult
    model_key: ClassVar[str] = "chapter"

    async def run(self, inp: dict[str, Any], *, model: str) -> ChapterResult:
        ch_id = chapter_id(inp)
        title = chapter_title(inp)
        return ChapterResult(
            chapter_id=ch_id,
            title=title,
            body_md=(
                f"# {title}\n\n"
                f"This M1 stub chapter was generated from `{inp.get('source_path', 'source')}`.\n\n"
                f"It preserves source traceability and gives downstream stages stable content."
            ),
            concepts=[f"{title} concept"],
            citations=[citation(inp)],
            owner_task_id=f"{ch_id}:chapter",
        )
