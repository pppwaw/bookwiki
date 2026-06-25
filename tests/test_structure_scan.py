from __future__ import annotations

import pytest

from bookwiki.pipeline.structure_scan import audit_coverage, scan_source_refs


def test_scan_source_refs_collects_all_ids() -> None:
    text = (
        "# 第1章\n\n<!-- source_ref:a-001 -->\n正文。\n\n"
        "## 1.1\n\n<!-- source_ref:a-002 -->\n更多正文。\n"
        "<!-- source_ref:a-003 -->\n"
    )
    assert scan_source_refs(text) == {"a-001", "a-002", "a-003"}


def test_scan_source_refs_empty_when_none() -> None:
    assert scan_source_refs("# 标题\n\n没有任何引用。\n") == set()


def test_audit_coverage_returns_empty_when_fully_covered() -> None:
    assert audit_coverage({"a", "b", "c"}, {"c", "b", "a"}) == []


def test_audit_coverage_lists_missing_refs_sorted() -> None:
    missing = audit_coverage({"a-003", "a-001", "a-002"}, {"a-001"})
    assert missing == ["a-002", "a-003"]


def test_audit_coverage_ignores_extra_covered_refs() -> None:
    # Covering a ref the source did not declare is not a coverage failure.
    assert audit_coverage({"a"}, {"a", "b"}) == []


@pytest.mark.parametrize("covered", [set(), {"x"}])
def test_audit_coverage_flags_total_loss(covered: set[str]) -> None:
    assert audit_coverage({"a", "b"}, covered) == ["a", "b"]
