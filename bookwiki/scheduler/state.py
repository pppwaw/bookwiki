"""Typed state schema for the LangGraph pipeline.

The legacy ``BookGraph`` passed a bare ``dict[str, Any]`` between nodes and merged
each node's returned delta with ``state.update(delta)`` (last value wins). LangGraph
only persists keys that are declared as channels, so this module enumerates every
top-level key any node may return. The default channel behaviour (overwrite) matches
the legacy ``state.update`` semantics exactly, so no custom reducers are needed for the
lift-and-shift; richer reducers arrive later when ``generate`` fans out.

Keys mirror ``NODE_OUTPUT_KEYS`` in ``bookwiki.scheduler.resume`` plus the control keys
(``book_id``, ``cache_hit``) and the two intermediate keys that the legacy graph kept
in state without listing them for ``--from``/``--force`` cleanup (``chapter_topics`` written by
``split`` and consumed by ``generate``; ``_repair_rounds`` carried across repair rounds).
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict


def merge_fanout_parts(
    current: dict[str, Any] | None, update: dict[str, Any] | None
) -> dict[str, Any]:
    """Merge worker results, with ``None`` resetting stale fanout state."""

    if update is None:
        return {}
    merged = dict(current or {})
    merged.update(update)
    return merged


class PipelineState(TypedDict, total=False):
    """All top-level channels carried through the book pipeline."""

    # --- control / identity ---
    book_id: str
    cache_hit: bool

    # --- convert ---
    sources_md: list[str]
    source_ref_manifests: list[str]

    # --- caption (also re-emits sources_md / source_ref_manifests) ---
    caption_results: list[Any]

    # --- structure ---
    proposed_structure: str
    approved_structure: str

    # --- split ---
    chapter_sources: dict[str, str]
    chapter_titles: dict[str, str]
    chapter_topics: dict[str, list[str]]
    chapter_groups: dict[str, Any]
    chapter_alignment: str
    chapter_split_report: str

    # --- build_skeleton ---
    skeleton: str

    # --- generate ---
    agent_results: dict[str, dict[str, str]]
    generation_issues: list[Any]
    generated_figures: dict[str, dict[str, str]]
    generated_figures_index: str
    _generate_parts: Annotated[dict[str, Any], merge_fanout_parts]
    _fanout_chapter_id: str
    _fanout_chapter_source: str

    # --- reconcile_concepts (also re-emits agent_results) ---
    reconciled_concepts: str
    alias_map: str

    # --- concept_pages ---
    concept_pages: Any
    concept_generation_issues: list[Any]
    _concept_page_parts: Annotated[dict[str, Any], merge_fanout_parts]
    _fanout_concept_order: int
    _fanout_concept_item: dict[str, Any]
    _fanout_concept_glossary: list[str]
    _fanout_concept_stem: str

    # --- integrate ---
    content_ready: bool
    content_index: str

    # --- check ---
    check_report: str
    repair_targets: list[str]

    # --- repair ---
    repairs: list[str]
    mdx_edited: list[str]
    repair_artifact_changed: bool
    repair_exhausted: list[Any]
    _repair_rounds: dict[str, int]

    # --- index ---
    sqlite: str


# Frozen view of every declared channel, used to assert parity with the legacy
# ``NODE_OUTPUT_KEYS`` and to drive ``--from``/``--force`` state cleanup.
STATE_KEYS: frozenset[str] = frozenset(PipelineState.__annotations__)
