from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.agents._helpers import chapter_id, chapter_title
from bookwiki.schemas.concept import ConceptCandidate


class ConceptExtractAgent:
    kind: ClassVar[str] = "concept_extract"
    output_model: ClassVar[type[ConceptCandidate]] = ConceptCandidate
    model_key: ClassVar[str] = "concept"

    async def run(self, inp: dict[str, Any], *, model: str) -> ConceptCandidate:
        ch_id = chapter_id(inp)
        name = f"{chapter_title(inp)} concept"
        return ConceptCandidate(
            name=name,
            aliases=[name.lower()],
            source_chapter_id=ch_id,
            owner_task_id=f"{ch_id}:concept_extract",
        )
