from __future__ import annotations

from typing import Any, ClassVar

from bookwiki.schemas.source import StructureResult


class StructureAgent:
    kind: ClassVar[str] = "structure_v2"
    output_model: ClassVar[type[StructureResult]] = StructureResult
    model_key: ClassVar[str] = "structure"

    async def run(
        self, inp: list[dict[str, Any]] | dict[str, Any], *, model: str
    ) -> StructureResult:
        summaries = inp.get("summaries", []) if isinstance(inp, dict) else inp
        chapters = _chapter_specs_from_sources(summaries)
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
    summaries: list[dict[str, Any]],
) -> list[tuple[str, str, list[str]]]:
    if not summaries:
        return [("ch01", "Foundations", []), ("ch02", "Practice", [])]
    if len(summaries) == 1 and len(summaries[0].get("source_refs") or []) > 1:
        refs = list(summaries[0].get("source_refs") or [])
        midpoint = max(1, len(refs) // 2)
        return [
            ("ch01", "Foundations", refs[:midpoint]),
            ("ch02", "Advanced Topics", refs[midpoint:]),
        ]

    chapters_by_id: dict[str, tuple[str, list[str]]] = {}
    chapter_order: list[str] = []
    used_ids: set[str] = set()
    for index, item in enumerate(summaries, start=1):
        source_id = str(item.get("source_id") or f"source-{index}")
        refs = list(item.get("source_refs") or [])
        detected_id = item.get("detected_chapter_id")
        chapter_id = str(detected_id) if detected_id else f"ch{index:02d}"
        if not detected_id and chapter_id in used_ids:
            chapter_id = f"ch{index:02d}"
        used_ids.add(chapter_id)
        title = str(item.get("detected_title") or _title_from_source_id(source_id))
        if chapter_id in chapters_by_id:
            existing_title, existing_refs = chapters_by_id[chapter_id]
            chapters_by_id[chapter_id] = (existing_title, [*existing_refs, *refs])
        else:
            chapter_order.append(chapter_id)
            chapters_by_id[chapter_id] = (title, refs)
    chapters = [
        (chapter_id, chapters_by_id[chapter_id][0], chapters_by_id[chapter_id][1])
        for chapter_id in chapter_order
    ]
    if len(chapters) == 1:
        chapters.append(("ch02", "Practice", []))
    return chapters


def _title_from_source_id(source_id: str) -> str:
    return source_id.replace("-", " ").replace("_", " ").title()
