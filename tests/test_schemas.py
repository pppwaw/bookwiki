from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from bookwiki.schemas import SCHEMA_VERSION
from bookwiki.schemas.card import CardItem, CardResult
from bookwiki.schemas.chapter import ChapterResult
from bookwiki.schemas.common import Citation
from bookwiki.schemas.concept import (
    ConceptCandidate,
    ConceptExtractResult,
    ConceptReconciledItem,
    ConceptReconcileResult,
    ConceptResult,
)
from bookwiki.schemas.quiz import QuizItem, QuizPlacement, QuizResult
from bookwiki.schemas.report import CheckReport, Issue
from bookwiki.schemas.source import (
    ChapterSplitResult,
    RepairResult,
    SourceLayoutPatch,
    SourceLayoutRepairResult,
    SourceSummaryResult,
    StructureResult,
)
from bookwiki.schemas.summary import SummaryResult

SCHEMA_SNAPSHOTS: list[tuple[type[BaseModel], dict, dict]] = [
    (
        Citation,
        {"ref_id": "source-p001", "quote": "source text"},
        {"ref_id": "source-p001", "quote": "source text"},
    ),
    (
        ChapterResult,
        {
            "chapter_id": "chapter-1",
            "title": "Search",
            "body_md": "# Search\n\nState space search.",
            "concepts": ["state space"],
            "citations": [{"ref_id": "source-p001", "quote": "State space"}],
            "owner_task_id": "chapter-1:chapter",
        },
        {
            "schema_version": SCHEMA_VERSION,
            "chapter_id": "chapter-1",
            "title": "Search",
            "body_md": "# Search\n\nState space search.",
            "concepts": ["state space"],
            "citations": [{"ref_id": "source-p001", "quote": "State space"}],
            "owner_task_id": "chapter-1:chapter",
        },
    ),
    (
        SummaryResult,
        {
            "chapter_id": "chapter-1",
            "summary_md": "Search explores states.",
            "key_points": ["States", "Goals"],
            "citations": [{"ref_id": "source-p001", "quote": "State space"}],
            "owner_task_id": "chapter-1:summary",
        },
        {
            "schema_version": SCHEMA_VERSION,
            "chapter_id": "chapter-1",
            "summary_md": "Search explores states.",
            "key_points": ["States", "Goals"],
            "citations": [{"ref_id": "source-p001", "quote": "State space"}],
            "owner_task_id": "chapter-1:summary",
        },
    ),
    (
        QuizItem,
        {
            "question": "What does search expand?",
            "choices": ["states", "pixels"],
            "answer": "states",
            "explanation": "Search expands states.",
            "citations": [{"ref_id": "source-p001", "quote": "State space"}],
        },
        {
            "schema_version": SCHEMA_VERSION,
            "question": "What does search expand?",
            "choices": ["states", "pixels"],
            "answer": "states",
            "explanation": "Search expands states.",
            "citations": [{"ref_id": "source-p001", "quote": "State space"}],
        },
    ),
    (
        QuizPlacement,
        {"after_block": 1, "item_indexes": [1], "title": "Checkpoint"},
        {
            "schema_version": SCHEMA_VERSION,
            "after_block": 1,
            "item_indexes": [1],
            "title": "Checkpoint",
        },
    ),
    (
        QuizResult,
        {
            "chapter_id": "chapter-1",
            "items": [
                {
                    "question": "What does search expand?",
                    "choices": ["states", "pixels"],
                    "answer": "states",
                    "explanation": "Search expands states.",
                    "citations": [{"ref_id": "source-p001", "quote": "State space"}],
                }
            ],
            "placements": [{"after_block": 1, "item_indexes": [1], "title": "Checkpoint"}],
            "owner_task_id": "chapter-1:quiz",
        },
        {
            "schema_version": SCHEMA_VERSION,
            "chapter_id": "chapter-1",
            "items": [
                {
                    "schema_version": SCHEMA_VERSION,
                    "question": "What does search expand?",
                    "choices": ["states", "pixels"],
                    "answer": "states",
                    "explanation": "Search expands states.",
                    "citations": [{"ref_id": "source-p001", "quote": "State space"}],
                }
            ],
            "placements": [
                {
                    "schema_version": SCHEMA_VERSION,
                    "after_block": 1,
                    "item_indexes": [1],
                    "title": "Checkpoint",
                }
            ],
            "owner_task_id": "chapter-1:quiz",
        },
    ),
    (
        CardItem,
        {
            "front": "State space",
            "back": "The set of reachable states.",
            "citations": [{"ref_id": "source-p001", "quote": "State space"}],
        },
        {
            "schema_version": SCHEMA_VERSION,
            "front": "State space",
            "back": "The set of reachable states.",
            "citations": [{"ref_id": "source-p001", "quote": "State space"}],
        },
    ),
    (
        CardResult,
        {
            "chapter_id": "chapter-1",
            "items": [
                {
                    "front": "State space",
                    "back": "The set of reachable states.",
                    "citations": [{"ref_id": "source-p001", "quote": "State space"}],
                }
            ],
            "owner_task_id": "chapter-1:card",
        },
        {
            "schema_version": SCHEMA_VERSION,
            "chapter_id": "chapter-1",
            "items": [
                {
                    "schema_version": SCHEMA_VERSION,
                    "front": "State space",
                    "back": "The set of reachable states.",
                    "citations": [{"ref_id": "source-p001", "quote": "State space"}],
                }
            ],
            "owner_task_id": "chapter-1:card",
        },
    ),
    (
        ConceptCandidate,
        {
            "name": "state space",
            "aliases": ["states"],
            "source_chapter_id": "chapter-1",
            "owner_task_id": "chapter-1:concept_extract",
        },
        {
            "schema_version": SCHEMA_VERSION,
            "name": "state space",
            "aliases": ["states"],
            "source_chapter_id": "chapter-1",
            "owner_task_id": "chapter-1:concept_extract",
        },
    ),
    (
        ConceptExtractResult,
        {
            "concepts": [
                {
                    "name": "state space",
                    "aliases": ["states"],
                    "source_chapter_id": "chapter-1",
                    "owner_task_id": "chapter-1:concept_extract",
                }
            ]
        },
        {
            "schema_version": SCHEMA_VERSION,
            "concepts": [
                {
                    "schema_version": SCHEMA_VERSION,
                    "name": "state space",
                    "aliases": ["states"],
                    "source_chapter_id": "chapter-1",
                    "owner_task_id": "chapter-1:concept_extract",
                }
            ],
        },
    ),
    (
        ConceptReconciledItem,
        {
            "canonical": "state space",
            "aliases": ["states"],
            "source_chapter_ids": ["chapter-1"],
        },
        {
            "schema_version": SCHEMA_VERSION,
            "canonical": "state space",
            "aliases": ["states"],
            "source_chapter_ids": ["chapter-1"],
        },
    ),
    (
        ConceptResult,
        {
            "name": "state space",
            "summary_md": "A reachable-state set.",
            "body_md": "State space is the set of reachable states.",
            "related": ["frontier"],
            "citations": [{"ref_id": "source-p001", "quote": "State space"}],
            "owner_task_id": "concept:state space",
        },
        {
            "schema_version": SCHEMA_VERSION,
            "name": "state space",
            "summary_md": "A reachable-state set.",
            "body_md": "State space is the set of reachable states.",
            "related": ["frontier"],
            "citations": [{"ref_id": "source-p001", "quote": "State space"}],
            "owner_task_id": "concept:state space",
        },
    ),
    (
        ConceptReconcileResult,
        {
            "concepts": [
                {
                    "canonical": "state space",
                    "aliases": ["states"],
                    "source_chapter_ids": ["chapter-1"],
                }
            ],
            "alias_map": {"states": "state space"},
        },
        {
            "schema_version": SCHEMA_VERSION,
            "concepts": [
                {
                    "schema_version": SCHEMA_VERSION,
                    "canonical": "state space",
                    "aliases": ["states"],
                    "source_chapter_ids": ["chapter-1"],
                }
            ],
            "alias_map": {"states": "state space"},
        },
    ),
    (
        SourceSummaryResult,
        {
            "source_id": "source",
            "summary_md": "Search notes.",
            "source_refs": ["source-p001"],
            "detected_chapter_id": "ch01",
            "detected_title": "Search",
            "headings": ["Chapter 1 Search"],
            "key_terms": ["state space"],
        },
        {
            "schema_version": SCHEMA_VERSION,
            "source_id": "source",
            "summary_md": "Search notes.",
            "source_refs": ["source-p001"],
            "detected_chapter_id": "ch01",
            "detected_title": "Search",
            "headings": ["Chapter 1 Search"],
            "key_terms": ["state space"],
        },
    ),
    (
        StructureResult,
        {
            "proposed_structure_yaml": "chapters:\n  - title: Chapter 1 Search\n",
            "chapters": ["Chapter 1 Search"],
        },
        {
            "schema_version": SCHEMA_VERSION,
            "proposed_structure_yaml": "chapters:\n  - title: Chapter 1 Search\n",
            "chapters": ["Chapter 1 Search"],
        },
    ),
    (
        ChapterSplitResult,
        {
            "chapters": {"chapter-1": "# Search"},
            "chapter_titles": {"chapter-1": "Search"},
            "alignment": [{"source_ref": "source-p001", "chapter_id": "chapter-1"}],
            "coverage": {"total_fragments": 1, "assigned_ratio": 1.0},
            "report_md": "# Split Audit",
        },
        {
            "schema_version": SCHEMA_VERSION,
            "chapters": {"chapter-1": "# Search"},
            "chapter_titles": {"chapter-1": "Search"},
            "alignment": [{"source_ref": "source-p001", "chapter_id": "chapter-1"}],
            "coverage": {"total_fragments": 1, "assigned_ratio": 1.0},
            "report_md": "# Split Audit",
        },
    ),
    (
        RepairResult,
        {
            "owner_task_id": "chapter-1:quiz",
            "action": "regenerate",
            "notes": "Quiz answer is missing from choices.",
        },
        {
            "schema_version": SCHEMA_VERSION,
            "owner_task_id": "chapter-1:quiz",
            "action": "regenerate",
            "notes": "Quiz answer is missing from choices.",
        },
    ),
    (
        SourceLayoutPatch,
        {
            "action": "attach_caption",
            "source_block_id": "source-p001-b002",
            "target_block_id": "source-p001-b001",
            "confidence": 0.91,
            "reason": "Caption follows image.",
        },
        {
            "schema_version": SCHEMA_VERSION,
            "action": "attach_caption",
            "source_block_id": "source-p001-b002",
            "target_block_id": "source-p001-b001",
            "confidence": 0.91,
            "reason": "Caption follows image.",
        },
    ),
    (
        SourceLayoutRepairResult,
        {
            "patches": [
                {
                    "action": "attach_caption",
                    "source_block_id": "source-p001-b002",
                    "target_block_id": "source-p001-b001",
                    "confidence": 0.91,
                    "reason": "Caption follows image.",
                }
            ],
            "notes": "One caption patch.",
        },
        {
            "schema_version": SCHEMA_VERSION,
            "patches": [
                {
                    "schema_version": SCHEMA_VERSION,
                    "action": "attach_caption",
                    "source_block_id": "source-p001-b002",
                    "target_block_id": "source-p001-b001",
                    "confidence": 0.91,
                    "reason": "Caption follows image.",
                }
            ],
            "notes": "One caption patch.",
        },
    ),
    (
        Issue,
        {
            "severity": "error",
            "code": "MISSING_SOURCE",
            "message": "Missing source.",
            "owner_task_id": "chapter-1:chapter",
        },
        {
            "schema_version": SCHEMA_VERSION,
            "severity": "error",
            "code": "MISSING_SOURCE",
            "message": "Missing source.",
            "owner_task_id": "chapter-1:chapter",
        },
    ),
    (
        CheckReport,
        {
            "status": "needs_repair",
            "issues": [
                {
                    "severity": "error",
                    "code": "MISSING_SOURCE",
                    "message": "Missing source.",
                    "owner_task_id": "chapter-1:chapter",
                }
            ],
        },
        {
            "schema_version": SCHEMA_VERSION,
            "status": "needs_repair",
            "issues": [
                {
                    "schema_version": SCHEMA_VERSION,
                    "severity": "error",
                    "code": "MISSING_SOURCE",
                    "message": "Missing source.",
                    "owner_task_id": "chapter-1:chapter",
                }
            ],
            "repair_targets": ["chapter-1:chapter"],
        },
    ),
]


@pytest.mark.parametrize(("model_cls", "payload", "snapshot"), SCHEMA_SNAPSHOTS)
def test_pydantic_models_match_fixture_snapshots(
    model_cls: type[BaseModel], payload: dict, snapshot: dict
) -> None:
    result = model_cls.model_validate(payload)

    assert result.model_dump(mode="json") == snapshot


def test_citation_requires_non_empty_fields() -> None:
    citation = Citation(ref_id="Prob_GZIC-p001", quote="A useful source sentence.")

    assert citation.ref_id == "Prob_GZIC-p001"
    assert citation.quote == "A useful source sentence."

    with pytest.raises(ValidationError):
        Citation(ref_id="", quote="missing ref")


def test_citation_ref_id_uses_validation_context_whitelist() -> None:
    citation = Citation.model_validate(
        {"ref_id": "source-p001", "quote": "source text"},
        context={"allowed_citation_refs": {"source-p001", "source-p002"}},
    )

    assert citation.ref_id == "source-p001"

    with pytest.raises(ValidationError, match="source-p999"):
        Citation.model_validate(
            {"ref_id": "source-p999", "quote": "source text"},
            context={"allowed_citation_refs": {"source-p001", "source-p002"}},
        )


def test_core_result_models_include_schema_version_and_owner() -> None:
    chapter = ChapterResult(
        chapter_id="ch01",
        title="Foundations",
        body_md="Chapter body",
        concepts=["state"],
        citations=[Citation(ref_id="Prob_GZIC-p001", quote="source text")],
        owner_task_id="ch01:chapter",
    )

    assert chapter.schema_version == SCHEMA_VERSION
    assert chapter.owner_task_id == "ch01:chapter"


def test_check_report_collects_repair_targets_from_issues() -> None:
    report = CheckReport(
        status="needs_repair",
        issues=[
            Issue(
                severity="error",
                code="MISSING_SOURCE",
                message="chapter needs a source",
                owner_task_id="ch01:chapter",
            )
        ],
    )

    assert report.repair_targets == ["ch01:chapter"]
