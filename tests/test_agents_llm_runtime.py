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
from bookwiki.scheduler.llm import TestLLMRuntime
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
                "proposed_structure_yaml": (
                    "chapters:\n"
                    "  - title: Chapter 6 Point Estimation\n"
                    "    topics:\n"
                    "      - Method of moments\n"
                    "    source_refs:\n"
                    "      - Week-10-p001\n"
                ),
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
        {
            "source_paths": [str(source)],
            "approved_structure": (
                "chapters:\n"
                "  - title: Chapter 6 Point Estimation\n"
                "    topics:\n"
                "      - Method of moments\n"
                "    source_refs:\n"
                "      - Week-10-p001\n"
            ),
        },
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


@pytest.mark.asyncio
async def test_quiz_and_card_agents_seed_requested_counts_from_config() -> None:
    payload = {
        "chapter_id": "chapter-1",
        "title": "Search",
        "source_md": "<!-- source_ref: source-p001 -->\nState space search.",
        "source_path": "work/chapter_sources/chapter-1/source.md",
        "language": "en-US",
        "quiz_per_chapter": 3,
        "cards_per_chapter": 4,
    }

    quiz = await QuizAgent().run(payload, model="deepseek-v4-pro", runtime=TestLLMRuntime())
    cards = await CardAgent().run(payload, model="deepseek-v4-flash", runtime=TestLLMRuntime())

    assert len(quiz.items) == 3
    assert len(cards.items) == 4


@pytest.mark.asyncio
async def test_content_agents_pass_allowed_refs_in_validation_context() -> None:
    payload = {
        "chapter_id": "chapter-1",
        "title": "Search",
        "source_md": "<!-- source_ref: source-p001 -->\nState space search.",
        "source_path": "work/chapter_sources/chapter-1/source.md",
        "language": "zh-CN",
    }
    runtime = RecordingRuntime(
        [
            {
                "chapter_id": "chapter-1",
                "title": "Search",
                "body_md": "# Search\n\nBody.",
                "concepts": ["state space"],
                "citations": [{"ref_id": "source-p001", "quote": "State"}],
                "owner_task_id": "chapter-1:chapter",
            }
        ]
    )

    await ChapterAgent().run(payload, model="deepseek-v4-pro", runtime=runtime)

    assert runtime.calls[0]["context"] == {"allowed_citation_refs": {"source-p001"}}
    assert runtime.calls[0]["max_retries"] == 2


@pytest.mark.asyncio
async def test_chapter_agent_wraps_source_as_document_chunks() -> None:
    payload = {
        "chapter_id": "chapter-1",
        "title": "Search",
        "source_md": (
            "# Search\n\n"
            "<!-- source_ref: source-p001 -->\n\n"
            "State space search.\n\n"
            "<!-- source_ref: source-p002 -->\n\n"
            "Heuristic search."
        ),
        "source_path": "work/chapter_sources/chapter-1/source.md",
        "language": "zh-CN",
    }
    runtime = RecordingRuntime(
        [
            {
                "chapter_id": "chapter-1",
                "title": "Search",
                "body_md": "# Search\n\nBody.",
                "concepts": ["state space"],
                "citations": [{"ref_id": "source-p001", "quote": "State"}],
                "owner_task_id": "chapter-1:chapter",
            }
        ]
    )

    await ChapterAgent().run(payload, model="deepseek-v4-pro", runtime=runtime)

    user_prompt = runtime.calls[0]["user"]
    assert "<document>" in user_prompt
    assert '<chunk ref="source-p001">' in user_prompt
    assert '<chunk ref="source-p002">' in user_prompt
    assert "State space search." in user_prompt


@pytest.mark.asyncio
async def test_agent_retries_when_llm_invents_citation_ref_id() -> None:
    payload = {
        "chapter_id": "chapter-1",
        "title": "Search",
        "source_md": "<!-- source_ref: source-p001 -->\nState space search.",
        "source_path": "work/chapter_sources/chapter-1/source.md",
        "language": "zh-CN",
    }
    runtime = RecordingRuntime(
        [
            {
                "chapter_id": "chapter-1",
                "title": "Search",
                "body_md": "# Search\n\nBody.",
                "concepts": ["state space"],
                "citations": [{"ref_id": "invented-p999", "quote": "State"}],
                "owner_task_id": "chapter-1:chapter",
            },
            {
                "chapter_id": "chapter-1",
                "title": "Search",
                "body_md": "# Search\n\nBody.",
                "concepts": ["state space"],
                "citations": [{"ref_id": "source-p001", "quote": "State"}],
                "owner_task_id": "chapter-1:chapter",
            },
        ]
    )

    result = await ChapterAgent().run(payload, model="deepseek-v4-pro", runtime=runtime)

    assert result.citations[0].ref_id == "source-p001"
    assert len(runtime.calls) == 2
    assert all(
        call["context"] == {"allowed_citation_refs": {"source-p001"}}
        for call in runtime.calls
    )


@pytest.mark.asyncio
async def test_agent_raises_after_repeated_invalid_citation_ref_ids() -> None:
    payload = {
        "chapter_id": "chapter-1",
        "title": "Search",
        "source_md": "<!-- source_ref: source-p001 -->\nState space search.",
        "source_path": "work/chapter_sources/chapter-1/source.md",
        "language": "zh-CN",
    }
    bad_response = {
        "chapter_id": "chapter-1",
        "title": "Search",
        "body_md": "# Search\n\nBody.",
        "concepts": ["state space"],
        "citations": [{"ref_id": "invented-p999", "quote": "State"}],
        "owner_task_id": "chapter-1:chapter",
    }
    runtime = RecordingRuntime([bad_response, bad_response])

    with pytest.raises(ValueError, match="invented-p999"):
        await ChapterAgent().run(payload, model="deepseek-v4-pro", runtime=runtime)

    assert len(runtime.calls) == 2
