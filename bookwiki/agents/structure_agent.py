from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.schemas.source import StructureResult


class StructureAgent:
    kind: ClassVar[str] = "structure"
    output_model: ClassVar[type[StructureResult]] = StructureResult
    model_key: ClassVar[str] = "structure"

    async def run(
        self, inp: list[dict[str, Any]] | dict[str, Any], *, model: str
    ) -> StructureResult:
        summaries = inp.get("summaries", []) if isinstance(inp, dict) else inp
        refs_by_source = [
            (str(item.get("source_id") or f"source-{index}"), list(item.get("source_refs") or []))
            for index, item in enumerate(summaries, start=1)
        ]
        chapters = _chapter_specs_from_sources(refs_by_source)
        lines = ["# Proposed Structure", ""]
        chapter_names: list[str] = []
        for index, (chapter_id, title, refs) in enumerate(chapters, start=1):
            chapter_names.append(f"{chapter_id} {title}")
            lines.extend(
                [
                    f"## {chapter_id} {title}",
                    "",
                    f"- 目标: Cover the source material assigned to {title}.",
                    f"- 范围: Automatically grouped source set {index}.",
                    "- 来源:",
                ]
            )
            lines.extend(f"  - {ref}" for ref in refs)
            lines.append("")
        return StructureResult(proposed_structure_md="\n".join(lines), chapters=chapter_names)


def _chapter_specs_from_sources(
    refs_by_source: list[tuple[str, list[str]]]
) -> list[tuple[str, str, list[str]]]:
    if not refs_by_source:
        return [("ch01", "Foundations", []), ("ch02", "Practice", [])]
    if len(refs_by_source) == 1 and len(refs_by_source[0][1]) > 1:
        refs = refs_by_source[0][1]
        midpoint = max(1, len(refs) // 2)
        return [
            ("ch01", "Foundations", refs[:midpoint]),
            ("ch02", "Advanced Topics", refs[midpoint:]),
        ]

    chapters: list[tuple[str, str, list[str]]] = []
    for index, (source_id, refs) in enumerate(refs_by_source, start=1):
        title = _title_from_source_id(source_id)
        chapters.append((f"ch{index:02d}", title, refs))
    if len(chapters) == 1:
        chapters.append(("ch02", "Practice", []))
    return chapters


def _title_from_source_id(source_id: str) -> str:
    return source_id.replace("-", " ").replace("_", " ").title()
