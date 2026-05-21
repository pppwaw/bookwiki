from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.schemas.source import StructureResult


class StructureAgent:
    kind: ClassVar[str] = "structure"
    output_model: ClassVar[type[StructureResult]] = StructureResult
    model_key: ClassVar[str] = "structure"

    async def run(self, inp: list[dict[str, Any]], *, model: str) -> StructureResult:
        return StructureResult(
            proposed_structure_md=(
                "# Approved Structure\n\n"
                "## ch01 Foundations\n"
                "- scope: first half of available source material\n"
                "- source_ref: Prob_GZIC-p001\n\n"
                "## ch02 Practice\n"
                "- scope: second half of available source material\n"
                "- source_ref: Prob_GZIC-p001\n"
            ),
            chapters=["ch01 Foundations", "ch02 Practice"],
        )
