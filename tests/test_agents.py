from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import litellm
import pytest
from pydantic import BaseModel

from bookwiki.agents import (
    ApplicationQuizAgent,
    CardAgent,
    ChapterSplitAgent,
    ConceptAgent,
    ConceptExtractAgent,
    ConceptReconcileAgent,
    ReviewAgent,
    SectionAgent,
    SourceLayoutRepairAgent,
    SourceSummaryAgent,
    StructureAgent,
    SummaryAgent,
    VisionCaptionAgent,
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
        image_paths: Sequence[str | Path] | None = None,
        max_retries: int = 2,
    ) -> BaseModel:
        self.calls.append(
            {
                "model": model,
                "output_model": output_model.__name__,
                "system": system,
                "user": user,
                "context": context,
                "image_paths": [str(path) for path in image_paths or []],
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

    async def generate_document(
        self,
        *,
        model: str,
        system: str,
        user: str,
        image_paths: Sequence[str | Path] | None = None,
        max_retries: int = 2,
    ) -> str:
        self.calls.append(
            {
                "model": model,
                "output_model": "Document",
                "system": system,
                "user": user,
                "context": None,
                "image_paths": [str(path) for path in image_paths or []],
                "max_retries": max_retries,
            }
        )
        response = self.responses.pop(0)
        body_field = (
            "summary_md" if "summary_md" in response and "body_md" not in response else "body_md"
        )
        body = response.get(body_field, "")
        frontmatter = {key: value for key, value in response.items() if key != body_field}
        import yaml

        frontmatter_text = yaml.safe_dump(frontmatter, allow_unicode=True, sort_keys=False).strip()
        return f"---\n{frontmatter_text}\n---\n{body}"


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
                "chapters": [
                    {
                        "title": "Chapter 1 Search",
                        "topics": ["State space search"],
                        "source_refs": ["source-p001"],
                    }
                ],
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
                "section_index": 0,
                "title": "Search",
                "body_md": "State space search explains reachable states.",
                "concepts": ["state space"],
                "citations": [{"ref_id": "source-p001", "quote": "State space search"}],
                "figure_requests": [],
                "owner_task_id": "chapter-1:section:000",
            },
            {
                "question": "Given $3$ states, what is expanded?",
                "choices": ["$3$ states", "$3$ colors"],
                "answer": "$3$ states",
                "explanation": "The source says search expands states.",
                "citations": [{"ref_id": "source-p001", "quote": "expands states"}],
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
                "chapter_id": "chapter-1",
                "summary_md": "Search explores states toward goals.",
                "key_points": ["States", "Goals"],
                "citations": [{"ref_id": "source-p001", "quote": "toward a goal"}],
                "owner_task_id": "chapter-1:summary",
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
                "summary_md": "A state space is the set of reachable states.",
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
            {
                "caption_md": "A diagram showing state expansion.",
                "key_points": ["states expand outward"],
                "source_ref": "source-p001",
                "confidence": 0.92,
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
    section = await SectionAgent().run(chapter_payload, model="deepseek-v4-pro", runtime=runtime)
    application_quiz = await ApplicationQuizAgent().run(
        {
            **chapter_payload,
            "chapter_body_md": section.body_md,
            "request": {
                "topic": "count expanded states",
                "concept": "search",
                "source_refs": ["source-p001"],
            },
        },
        model="deepseek-v4-pro",
        runtime=runtime,
    )
    card = await CardAgent().run(
        {**chapter_payload, "chapter_body_md": section.body_md},
        model="deepseek-v4-flash",
        runtime=runtime,
    )
    summary = await SummaryAgent().run(chapter_payload, model="deepseek-v4-flash", runtime=runtime)
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
                    "citations": [item.model_dump(mode="json") for item in section.citations],
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
    figure = tmp_path / "figure.png"
    figure.write_bytes(b"image-bytes")
    vision = await VisionCaptionAgent().run(
        {
            "block_id": "source-p001-b003",
            "source_ref": "source-p001",
            "asset_path": "work/assets/source/figure.png",
            "asset_full_path": str(figure),
            "nearby_text": "State space search expands states.",
        },
        model="kimi-k2.6",
        runtime=runtime,
    )

    assert source_summary.source_refs == ["source-p001"]
    assert [chapter.title for chapter in structure.chapters] == ["Chapter 1 Search"]
    assert split.report_md == "# Split Audit\n\nMock audit."
    assert section.owner_task_id == "chapter-1:section:000"
    assert application_quiz.answer == "$3$ states"
    assert card.items[0].front == "State space search"
    assert summary.key_points == ["States", "Goals"]
    assert extracted.concepts[0].name == "state space"
    assert reconciled.alias_map["states"] == "state space"
    assert concept.owner_task_id == "concept:state space"
    assert concept.summary_md
    assert repair.action == "regenerate"
    assert layout.patches[0].action == "attach_caption"
    assert vision.caption_md == "A diagram showing state expansion."
    assert runtime.calls[-1]["image_paths"] == [str(figure)]
    assert len(runtime.calls) == 12
    assert runtime.responses == []
    assert {call["output_model"] for call in runtime.calls} >= {
        "QuizItem",
        "CardResult",
        "SourceLayoutRepairResult",
        "VisionCaptionResult",
    }
