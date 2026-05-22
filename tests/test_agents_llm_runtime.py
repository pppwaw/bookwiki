from __future__ import annotations

import pytest

from bookwiki.agents import (
    CardAgent,
    ChapterAgent,
    ChapterSplitAgent,
    ConceptAgent,
    ConceptExtractAgent,
    ConceptReconcileAgent,
    QuizAgent,
    ReviewAgent,
    SourceSummaryAgent,
    StructureAgent,
    SummaryAgent,
)
from tests.fakes import RecordingRuntime


@pytest.mark.asyncio
async def test_all_agents_call_llm_runtime(tmp_path) -> None:
    source = tmp_path / "Week-10.md"
    source.write_text(
        "# Chapter 6 The point estimation\n\n"
        "<!-- source_ref: Week-10-p001 -->\n\n"
        "method of moments",
        encoding="utf-8",
    )
    runtime = RecordingRuntime(
        [
            {
                "source_id": "Week-10",
                "summary_md": "Point estimation covers method of moments.",
                "source_refs": ["Week-10-p001"],
                "detected_chapter_id": "ch06",
                "detected_title": "Point Estimation",
                "headings": ["Chapter 6 The point estimation"],
                "key_terms": ["method of moments"],
            },
            {
                "proposed_structure_md": "# Proposed Structure\n\n## Chapter 6 Point Estimation",
                "chapters": ["Chapter 6 Point Estimation"],
            },
            {
                "chapters": {"chapter-6": "# Chapter 6 Point Estimation"},
                "chapter_titles": {"chapter-6": "Point Estimation"},
                "alignment": [],
                "coverage": {"total_fragments": 0, "assigned_ratio": 1.0},
                "report_md": "# Split Audit\n\nLLM reviewed deterministic split.",
            },
            {
                "chapter_id": "chapter-6",
                "title": "Point Estimation",
                "body_md": "# Point Estimation\n\nGenerated chapter.",
                "concepts": ["point estimation"],
                "citations": [{"ref_id": "Week-10-p001", "quote": "method of moments"}],
                "owner_task_id": "chapter-6:chapter",
            },
            {
                "chapter_id": "chapter-6",
                "summary_md": "Summary.",
                "key_points": ["Point estimation"],
                "citations": [{"ref_id": "Week-10-p001", "quote": "method of moments"}],
                "owner_task_id": "chapter-6:summary",
            },
            {
                "chapter_id": "chapter-6",
                "items": [
                    {
                        "question": "What is estimated?",
                        "choices": ["parameter", "path"],
                        "answer": "parameter",
                        "explanation": "Point estimation estimates parameters.",
                        "citations": [{"ref_id": "Week-10-p001", "quote": "unknown parameters"}],
                    }
                ],
                "owner_task_id": "chapter-6:quiz",
            },
            {
                "chapter_id": "chapter-6",
                "items": [
                    {
                        "front": "Point estimation",
                        "back": "Estimate unknown parameters.",
                        "citations": [{"ref_id": "Week-10-p001", "quote": "unknown parameters"}],
                    }
                ],
                "owner_task_id": "chapter-6:card",
            },
            {
                "name": "point estimation",
                "aliases": ["estimator"],
                "source_chapter_id": "chapter-6",
                "owner_task_id": "chapter-6:concept_extract",
            },
            {
                "concepts": [
                    {
                        "canonical": "point estimation",
                        "aliases": ["estimator"],
                        "source_chapter_ids": ["chapter-6"],
                    }
                ],
                "alias_map": {"estimator": "point estimation"},
            },
            {
                "name": "point estimation",
                "body_md": "Concept page.",
                "related": [],
                "citations": [{"ref_id": "Week-10-p001", "quote": "point estimation"}],
                "owner_task_id": "concept:point estimation",
            },
            {
                "owner_task_id": "chapter-6:chapter",
                "action": "revise",
                "notes": "LLM repair plan.",
            },
        ]
    )
    chapter_payload = {
        "chapter_id": "chapter-6",
        "title": "Point Estimation",
        "source_md": "<!-- source_ref: Week-10-p001 -->\nmethod of moments",
        "source_path": "work/chapter_sources/chapter-6/source.md",
    }

    await SourceSummaryAgent().run(source, model="deepseek-v4-flash", runtime=runtime)
    await StructureAgent().run({"summaries": []}, model="deepseek-v4-pro", runtime=runtime)
    await ChapterSplitAgent().run(
        {"source_paths": [str(source)], "approved_structure": "## Chapter 6 Point Estimation"},
        model="deepseek-v4-flash",
        runtime=runtime,
    )
    await ChapterAgent().run(chapter_payload, model="deepseek-v4-pro", runtime=runtime)
    await SummaryAgent().run(chapter_payload, model="deepseek-v4-flash", runtime=runtime)
    await QuizAgent().run(chapter_payload, model="deepseek-v4-pro", runtime=runtime)
    await CardAgent().run(chapter_payload, model="deepseek-v4-flash", runtime=runtime)
    await ConceptExtractAgent().run(chapter_payload, model="deepseek-v4-flash", runtime=runtime)
    await ConceptReconcileAgent().run(
        [{"name": "point estimation", "source_chapter_id": "chapter-6"}],
        model="deepseek-v4-flash",
        runtime=runtime,
    )
    await ConceptAgent().run(
        {"canonical": "point estimation", "source_chapter_ids": ["chapter-6"]},
        model="deepseek-v4-flash",
        runtime=runtime,
    )
    await ReviewAgent().run(
        {"owner_task_id": "chapter-6:chapter"},
        model="deepseek-v4-pro",
        runtime=runtime,
    )

    assert len(runtime.calls) == 11
    assert all("Return valid JSON" in call["system"] for call in runtime.calls)
