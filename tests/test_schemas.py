from __future__ import annotations

import pytest
from pydantic import ValidationError

from bookwiki.schemas import SCHEMA_VERSION
from bookwiki.schemas.chapter import ChapterResult
from bookwiki.schemas.common import Citation
from bookwiki.schemas.report import CheckReport, Issue


def test_citation_requires_non_empty_fields() -> None:
    citation = Citation(ref_id="Prob_GZIC-p001", quote="A useful source sentence.")

    assert citation.ref_id == "Prob_GZIC-p001"
    assert citation.quote == "A useful source sentence."

    with pytest.raises(ValidationError):
        Citation(ref_id="", quote="missing ref")


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
