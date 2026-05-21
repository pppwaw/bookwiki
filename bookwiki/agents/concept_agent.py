from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.schemas.common import Citation
from bookwiki.schemas.concept import ConceptResult


class ConceptAgent:
    kind: ClassVar[str] = "concept"
    output_model: ClassVar[type[ConceptResult]] = ConceptResult
    model_key: ClassVar[str] = "concept"

    async def run(self, inp: dict[str, Any], *, model: str) -> ConceptResult:
        name = str(inp.get("canonical") or inp.get("name") or "Concept")
        chapters = [str(ch) for ch in inp.get("source_chapter_ids", ["ch01"])]
        return ConceptResult(
            name=name,
            body_md=f"{name} is a reconciled concept linked from {', '.join(chapters)}.",
            related=[],
            citations=[Citation(ref_id="Prob_GZIC-p001", quote="concept source")],
            owner_task_id=f"concept:{name}",
        )
