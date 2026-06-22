"""Post-integrate stitching checks (Phase 6 / M5 of the LangGraph + agentic plan).

The integrator already *performs* the stitching while rendering the vault: it
converges terminology (every ``[[alias]]`` wikilink is rewritten to its canonical
name) and resolves cross-chapter concept mentions into ``<PreviewLink>`` tags that
point at the owning concept page (see ``_normalize_concept_links`` in
``bookwiki.pipeline.nodes``).

This module makes those guarantees *observable and regression-testable*. It does
not mutate content; it audits the already-rendered vault and reports:

- **term drift** — any ``[[alias]]`` wikilink that leaked through un-normalized
  (its label maps to a different canonical in ``alias_map``). A correctly stitched
  vault has zero drift because every wikilink has converged to its canonical.
- **unresolved cross-references** — any ``<PreviewLink href=...>`` whose target
  concept slug has no page in ``concepts/``, plus any residual bare ``[[name]]``
  wikilink (a bare wikilink means no concept page was linked at all).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
# PreviewLink hrefs render as ``href={"/docs/concepts/<slug>"}`` (JSX expression form). Chapter
# cross-links share the same ``<PreviewLink>`` shape but point at ``/docs/chapters/<doc_slug>``.
_PREVIEW_HREF_RE = re.compile(r'href=\{?"[^"]*?concepts/([^"]+?)"\}?')
_CHAPTER_HREF_RE = re.compile(r'href=\{?"[^"]*?chapters/([^"]+?)"\}?')


def _concept_key(value: str) -> str:
    """Normalize a concept label for comparison.

    Matches :func:`bookwiki.agents.concept_reconcile._concept_key` exactly so the
    audit uses the same identity the reconciler/integrator used to converge terms.
    """
    return re.sub(r"[\W_]+", "", str(value).casefold(), flags=re.UNICODE)


def find_term_drift(mdx: str, alias_map: dict[str, str]) -> list[str]:
    """Return wikilink labels that leaked through without converging to canonical.

    After integration every ``[[X]]`` must have been rewritten to its canonical
    name. A residual ``[[X]]`` whose label resolves (via ``alias_map``) to a
    *different* canonical is term drift.
    """
    drift: list[str] = []
    for match in _WIKILINK_RE.finditer(mdx):
        label = match.group(1).strip()
        canonical = alias_map.get(label) or alias_map.get(_concept_key(label))
        if canonical and canonical != label:
            drift.append(label)
    return drift


def find_unresolved_concept_links(
    mdx: str,
    concept_slugs: set[str],
    chapter_slugs: set[str] | None = None,
) -> list[str]:
    """Return cross-reference targets that do not resolve to a page.

    Three kinds of dangling references are reported:
    - a concept ``<PreviewLink>`` whose ``href`` slug is not among ``concept_slugs``;
    - a chapter ``<PreviewLink>`` whose ``href`` slug is not among ``chapter_slugs``
      (only checked when ``chapter_slugs`` is provided — a chapter-to-chapter link);
    - a bare ``[[name]]`` wikilink (it was never turned into a PreviewLink, so no page backs it).
    """
    unresolved: list[str] = []
    for match in _PREVIEW_HREF_RE.finditer(mdx):
        slug = match.group(1).strip()
        if slug and slug not in concept_slugs:
            unresolved.append(slug)
    if chapter_slugs is not None:
        for match in _CHAPTER_HREF_RE.finditer(mdx):
            slug = match.group(1).strip()
            if slug and slug not in chapter_slugs:
                unresolved.append(slug)
    for match in _WIKILINK_RE.finditer(mdx):
        unresolved.append(f"[[{match.group(1).strip()}]]")
    return unresolved


@dataclass(frozen=True)
class StitchingReport:
    """Aggregate stitching audit over a rendered vault.

    ``term_drift`` / ``unresolved_xrefs`` are ``(relative_path, target)`` pairs so
    failures point at the offending file.
    """

    term_drift: list[tuple[str, str]] = field(default_factory=list)
    unresolved_xrefs: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.term_drift and not self.unresolved_xrefs


def audit_stitching(content_dir: Path, alias_map: dict[str, str]) -> StitchingReport:
    """Audit a rendered vault for term drift and unresolved cross-references.

    ``content_dir`` is the docs root (it contains ``chapters/`` and ``concepts/``).
    Concept slugs are taken from the rendered ``concepts/*.mdx`` filenames; chapter slugs from the
    ``chapters/**/*.mdx`` paths (relative, without suffix, so a nested ``group/leaf`` matches the
    ``/docs/chapters/group/leaf`` href) — exactly what a ``<PreviewLink href>`` points at.
    """
    concepts_dir = content_dir / "concepts"
    concept_slugs = {path.stem for path in concepts_dir.glob("*.mdx")}
    chapters_dir = content_dir / "chapters"
    chapter_slugs = (
        {
            path.relative_to(chapters_dir).with_suffix("").as_posix()
            for path in chapters_dir.rglob("*.mdx")
        }
        if chapters_dir.exists()
        else set()
    )

    drift: list[tuple[str, str]] = []
    unresolved: list[tuple[str, str]] = []
    for mdx_path in sorted(content_dir.rglob("*.mdx")):
        rel = str(mdx_path.relative_to(content_dir))
        text = mdx_path.read_text(encoding="utf-8")
        drift.extend((rel, label) for label in find_term_drift(text, alias_map))
        unresolved.extend(
            (rel, target)
            for target in find_unresolved_concept_links(text, concept_slugs, chapter_slugs)
        )
    return StitchingReport(term_drift=drift, unresolved_xrefs=unresolved)
