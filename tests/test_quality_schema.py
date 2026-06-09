from __future__ import annotations

import pytest
from pydantic import ValidationError

from bookwiki.schemas.quality import QualityFinding, QualityReport


def test_quality_finding_accepts_language_leak() -> None:
    finding = QualityFinding(
        category="language_leak",
        quote="查得select the cutoff value",
        explanation="中英粘连",
    )

    assert finding.category == "language_leak"
    assert finding.quote == "查得select the cutoff value"


def test_quality_finding_rejects_other_categories() -> None:
    with pytest.raises(ValidationError):
        QualityFinding(category="style", quote="x", explanation="not supported")


def test_quality_report_defaults_to_empty_findings() -> None:
    report = QualityReport(owner_task_id="chapter-1:chapter")

    assert report.findings == []
