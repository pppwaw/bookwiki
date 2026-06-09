"""Phase 6 / M5 stitching-layer audit tests.

The integrator converges terminology and resolves cross-chapter concept mentions
into ``<PreviewLink>`` tags while rendering. These tests pin those guarantees:
a correctly stitched vault has **zero term drift** and **zero unresolved
cross-references**, and the audit catches both kinds of regression.
"""

from __future__ import annotations

from pathlib import Path

from bookwiki.integrator.stitching import (
    audit_stitching,
    find_term_drift,
    find_unresolved_concept_links,
)

ALIAS_MAP = {
    "Random sample (随机样本)": "Random sample (随机样本)",
    "随机样本": "Random sample (随机样本)",
    "random sample": "Random sample (随机样本)",
}


# --------------------------------------------------------------------------- #
# find_term_drift
# --------------------------------------------------------------------------- #
def test_term_drift_flags_unnormalized_alias() -> None:
    # A residual wikilink whose label maps to a different canonical = drift.
    drift = find_term_drift("先抽取一个 [[随机样本]]，再计算统计量。", ALIAS_MAP)
    assert drift == ["随机样本"]


def test_term_drift_empty_when_converged() -> None:
    # Already-canonical wikilink and prose without wikilinks are not drift.
    assert find_term_drift("先抽取一个 [[Random sample (随机样本)]]。", ALIAS_MAP) == []
    assert find_term_drift("一段普通正文，没有任何 wikilink。", ALIAS_MAP) == []


def test_term_drift_ignores_unknown_labels() -> None:
    # A wikilink not in the alias map is not drift (nothing to converge to).
    assert find_term_drift("[[完全未知的词]]", ALIAS_MAP) == []


# --------------------------------------------------------------------------- #
# find_unresolved_concept_links
# --------------------------------------------------------------------------- #
SLUGS = {"Random-sample-随机样本", "Statistic-统计量"}


def test_unresolved_flags_dangling_preview_href() -> None:
    mdx = (
        '理解 <PreviewLink href={"../concepts/Does-not-exist-不存在"} '
        'title={"x"}>X</PreviewLink>。'
    )
    assert find_unresolved_concept_links(mdx, SLUGS) == ["Does-not-exist-不存在"]


def test_unresolved_flags_bare_wikilink() -> None:
    # A bare wikilink means no concept page was linked at all.
    assert find_unresolved_concept_links("见 [[随机样本]]。", SLUGS) == ["[[随机样本]]"]


def test_unresolved_empty_when_href_resolves() -> None:
    mdx = (
        '理解 <PreviewLink href={"../concepts/Random-sample-随机样本"} '
        'title={"x"}>样本</PreviewLink>。'
    )
    assert find_unresolved_concept_links(mdx, SLUGS) == []


# --------------------------------------------------------------------------- #
# audit_stitching (vault level)
# --------------------------------------------------------------------------- #
def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _vault(tmp_path: Path) -> Path:
    content = tmp_path / "content" / "docs"
    _write(content / "concepts" / "Random-sample-随机样本.mdx", "# 随机样本\n\n定义。\n")
    return content


def test_audit_clean_vault_is_ok(tmp_path: Path) -> None:
    content = _vault(tmp_path)
    _write(
        content / "chapters" / "chapter-1.mdx",
        '# 第一章\n\n抽取一个 <PreviewLink '
        'href={"../concepts/Random-sample-随机样本"} '
        'title={"x"}>随机样本</PreviewLink>。\n',
    )
    report = audit_stitching(content, ALIAS_MAP)
    assert report.ok
    assert report.term_drift == []
    assert report.unresolved_xrefs == []


def test_audit_detects_drift_and_dangling_xref(tmp_path: Path) -> None:
    content = _vault(tmp_path)
    # Un-normalized wikilink (drift) AND a PreviewLink to a missing concept page.
    _write(
        content / "chapters" / "chapter-1.mdx",
        '# 第一章\n\n见 [[随机样本]] 与 '
        '<PreviewLink href={"../concepts/Missing-缺页"} title={"x"}>缺页</PreviewLink>。\n',
    )
    report = audit_stitching(content, ALIAS_MAP)
    assert not report.ok
    assert ("chapters/chapter-1.mdx", "随机样本") in report.term_drift
    assert ("chapters/chapter-1.mdx", "Missing-缺页") in report.unresolved_xrefs
    # The bare wikilink is also reported as an unresolved cross-reference.
    assert ("chapters/chapter-1.mdx", "[[随机样本]]") in report.unresolved_xrefs
