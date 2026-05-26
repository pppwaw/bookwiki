from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import litellm
import pytest
from pydantic import BaseModel

from bookwiki.agents import (
    CardAgent,
    ChapterAgent,
    ChapterSplitAgent,
    ConceptAgent,
    ConceptExtractAgent,
    ConceptReconcileAgent,
    QuizAgent,
    ReviewAgent,
    SourceLayoutRepairAgent,
    SourceSummaryAgent,
    StructureAgent,
    SummaryAgent,
)


class LitellmMockRuntime:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        *,
        model: str,
        output_model: type[BaseModel],
        system: str,
        user: str,
        context: dict[str, Any] | None = None,
        max_retries: int = 2,
    ) -> BaseModel:
        self.calls.append(
            {
                "model": model,
                "output_model": output_model.__name__,
                "system": system,
                "user": user,
                "context": context,
                "max_retries": max_retries,
            }
        )
        response = self.responses.pop(0)
        mocked = litellm.completion(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            mock_response=json.dumps(response),
        )
        content = mocked.choices[0].message.content
        return output_model.model_validate(json.loads(content), context=context)


@pytest.mark.asyncio
async def test_all_agents_run_with_litellm_mock_response(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text(
        "# Chapter 1 Search\n\n"
        "<!-- source_ref: source-p001 -->\n\n"
        "State space search expands states toward a goal.",
        encoding="utf-8",
    )
    runtime = LitellmMockRuntime(
        [
            {
                "source_id": "source",
                "summary_md": "Search source summary.",
                "source_refs": ["source-p001"],
                "detected_chapter_id": "ch01",
                "detected_title": "Search",
                "headings": ["Chapter 1 Search"],
                "key_terms": ["state space"],
            },
            {
                "proposed_structure_yaml": (
                    "chapters:\n"
                    "  - title: Chapter 1 Search\n"
                    "    topics:\n"
                    "      - State space search\n"
                    "    source_refs:\n"
                    "      - source-p001\n"
                ),
                "chapters": ["Chapter 1 Search"],
            },
            {
                "chapters": {"chapter-1": "# Chapter 1 Search\n\nState space search."},
                "chapter_titles": {"chapter-1": "Search"},
                "alignment": [],
                "coverage": {"total_fragments": 1, "assigned_ratio": 1.0},
                "report_md": "# Split Audit\n\nMock audit.",
            },
            {
                "chapter_id": "chapter-1",
                "title": "Search",
                "body_md": "# Search\n\nState space search explains reachable states.",
                "concepts": ["state space"],
                "citations": [{"ref_id": "source-p001", "quote": "State space search"}],
                "owner_task_id": "chapter-1:chapter",
            },
            {
                "chapter_id": "chapter-1",
                "summary_md": "Search explores states toward goals.",
                "key_points": ["States", "Goals"],
                "citations": [{"ref_id": "source-p001", "quote": "toward a goal"}],
                "owner_task_id": "chapter-1:summary",
            },
            {
                "chapter_id": "chapter-1",
                "items": [
                    {
                        "question": "What does search expand?",
                        "choices": ["states", "colors"],
                        "answer": "states",
                        "explanation": "The source says search expands states.",
                        "citations": [{"ref_id": "source-p001", "quote": "expands states"}],
                    }
                ],
                "placements": [{"after_block": 0, "item_indexes": [1], "title": "Quiz"}],
                "owner_task_id": "chapter-1:quiz",
            },
            {
                "chapter_id": "chapter-1",
                "items": [
                    {
                        "front": "State space search",
                        "back": "Search over reachable states toward a goal.",
                        "citations": [{"ref_id": "source-p001", "quote": "toward a goal"}],
                    }
                ],
                "owner_task_id": "chapter-1:card",
            },
            {
                "concepts": [
                    {
                        "canonical": "state space",
                        "aliases": ["states"],
                        "source_chapter_ids": ["chapter-1"],
                    }
                ],
                "alias_map": {"state space": "state space", "states": "state space"},
            },
            {
                "name": "state space",
                "body_md": "A state space contains reachable states.",
                "related": [],
                "citations": [{"ref_id": "source-p001", "quote": "State space"}],
                "owner_task_id": "concept:state space",
            },
            {
                "owner_task_id": "chapter-1:quiz",
                "action": "regenerate",
                "notes": "Regenerate quiz choices.",
            },
            {
                "patches": [
                    {
                        "action": "attach_caption",
                        "source_block_id": "source-p001-b002",
                        "target_block_id": "source-p001-b001",
                        "confidence": 0.91,
                        "reason": "Caption follows figure.",
                    }
                ],
                "notes": "Attach one caption.",
            },
        ]
    )
    chapter_payload = {
        "chapter_id": "chapter-1",
        "title": "Search",
        "source_md": source.read_text(encoding="utf-8"),
        "source_path": str(source),
        "concepts": ["state space"],
    }

    source_summary = await SourceSummaryAgent().run(
        source, model="deepseek-v4-flash", runtime=runtime
    )
    structure = await StructureAgent().run(
        {"summaries": [source_summary.model_dump(mode="json")]},
        model="deepseek-v4-pro",
        runtime=runtime,
    )
    split = await ChapterSplitAgent().run(
        {
            "source_paths": [str(source)],
            "approved_structure": structure.proposed_structure_yaml,
        },
        model="deepseek-v4-flash",
        runtime=runtime,
    )
    chapter = await ChapterAgent().run(
        chapter_payload, model="deepseek-v4-pro", runtime=runtime
    )
    summary = await SummaryAgent().run(
        chapter_payload, model="deepseek-v4-flash", runtime=runtime
    )
    quiz = await QuizAgent().run(chapter_payload, model="deepseek-v4-pro", runtime=runtime)
    cards = await CardAgent().run(
        chapter_payload, model="deepseek-v4-flash", runtime=runtime
    )
    extracted = await ConceptExtractAgent().run(
        chapter_payload, model="deepseek-v4-flash", runtime=runtime
    )
    reconciled = await ConceptReconcileAgent().run(
        [item.model_dump(mode="json") for item in extracted.concepts],
        model="deepseek-v4-pro",
        runtime=runtime,
    )
    concept = await ConceptAgent().run(
        {
            "canonical": "state space",
            "source_chapter_ids": ["chapter-1"],
            "chapter_contexts": [
                    {
                        "chapter_id": "chapter-1",
                        "title": "Search",
                        "source_md": chapter_payload["source_md"],
                        "citations": [
                            item.model_dump(mode="json") for item in chapter.citations
                        ],
                    }
                ],
            },
        model="deepseek-v4-pro",
        runtime=runtime,
    )
    repair = await ReviewAgent().run(
        {"owner_task_id": "chapter-1:quiz"}, model="deepseek-v4-pro", runtime=runtime
    )
    layout = await SourceLayoutRepairAgent().run(
        {
            "candidates": [
                {
                    "source_block_id": "source-p001-b002",
                    "target_block_id": "source-p001-b001",
                }
            ]
        },
        model="deepseek-v4-flash",
        runtime=runtime,
    )

    assert source_summary.source_refs == ["source-p001"]
    assert structure.chapters == ["Chapter 1 Search"]
    assert split.report_md == "# Split Audit\n\nMock audit."
    assert chapter.owner_task_id == "chapter-1:chapter"
    assert summary.key_points == ["States", "Goals"]
    assert quiz.items[0].answer == "states"
    assert cards.items[0].front == "State space search"
    assert extracted.concepts[0].name == "state space"
    assert reconciled.alias_map["states"] == "state space"
    assert concept.owner_task_id == "concept:state space"
    assert repair.action == "regenerate"
    assert layout.patches[0].action == "attach_caption"
    assert len(runtime.calls) == 11
    assert runtime.responses == []
    assert {call["output_model"] for call in runtime.calls} >= {
        "ChapterResult",
        "QuizResult",
        "SourceLayoutRepairResult",
    }
