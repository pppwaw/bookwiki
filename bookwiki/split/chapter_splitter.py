from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from bookwiki.convert.common import SOURCE_REF_RE, slugify_path_segment

# The catch-all bucket id for unassigned source fragments; reserved so no chapter title claims it.
APPENDIX_CHAPTER_ID = "appendix"


@dataclass(frozen=True)
class ChapterSpec:
    chapter_id: str
    title: str
    topics: list[str] = field(default_factory=list)
    source_refs: list[str] = field(default_factory=list)
    # When the chapter belongs to a two-level group, ``group_id`` is the parent
    # group's id (e.g. ``chapter-9``) and ``group_title`` its display title
    # (e.g. ``Chapter 9 Infinite Series``). ``None`` for flat (ungrouped) chapters.
    group_id: str | None = None
    group_title: str | None = None


@dataclass(frozen=True)
class SourceFragment:
    source_path: str
    source_id: str
    source_ref: str
    body: str


@dataclass(frozen=True)
class SplitResult:
    chapters: dict[str, str]
    chapter_titles: dict[str, str]
    alignment: list[dict[str, object]]
    coverage: dict[str, float | int]
    report_md: str
    chapter_groups: dict[str, dict[str, object]] = field(default_factory=dict)
    # Authoritative reading order: rendered chapter ids in approved-structure (YAML) order,
    # appendix last. Persisted so resume never has to reconstruct order from a directory glob.
    chapter_order: list[str] = field(default_factory=list)
    # Declared source_refs per chapter id (from the approved structure), used to fingerprint a
    # chapter's identity for the persisted slug registry (so same-titled chapters stay distinct).
    chapter_source_refs: dict[str, list[str]] = field(default_factory=dict)


def parse_approved_structure(structure_yaml: str) -> list[ChapterSpec]:
    """Parse the approved structure YAML into a flat, depth-first list of leaf chapters.

    Two shapes are accepted per top-level ``chapters`` entry:

    * **Flat leaf**: ``{title, topics, source_refs}``.
    * **Group** (two-level): ``{title, sections: [<section>, ...]}`` where each section is
      ``{title, topics, source_refs}``. Sections are flattened into leaf ``ChapterSpec``s carrying
      ``group_id``/``group_title``; reading order is preserved.

    A group entry must not also carry ``topics``/``source_refs``. Titles are free-form (any text,
    including CJK, with or without a "Chapter N" prefix); ``topics``/``source_refs`` stay required.

    Each chapter's ``chapter_id`` is derived deterministically from its ``title`` via
    :func:`bookwiki.convert.common.slugify_path_segment` (CJK preserved), then de-duplicated within
    the book by appending ``-2``, ``-3`` … to later collisions in reading order. The id becomes the
    chapter's directory name and site URL slug. The ``appendix`` id is reserved for the
    unassigned-fragment bucket, so no title may claim it.
    """
    try:
        payload = yaml.safe_load(structure_yaml)
    except yaml.YAMLError as exc:
        msg = "approved structure must be YAML with a top-level chapters list"
        raise ValueError(msg) from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("chapters"), list):
        msg = "approved structure must be YAML with a top-level chapters list"
        raise ValueError(msg)
    chapters: list[ChapterSpec] = []
    # ``used_ids`` holds the casefold of every assigned id (chapter and group), so dedup is
    # case-insensitive (safe on case-insensitive FS); seed it with the reserved appendix id.
    used_ids: set[str] = {APPENDIX_CHAPTER_ID}
    for index, item in enumerate(payload["chapters"], start=1):
        if not isinstance(item, dict):
            msg = f"chapter entry {index} must be a mapping"
            raise ValueError(msg)
        if item.get("sections") is not None:
            chapters.extend(_parse_group(item, index, used_ids))
        else:
            chapters.append(_parse_leaf(item, used_ids))
    if not chapters:
        msg = "approved structure must contain at least one chapter"
        raise ValueError(msg)
    _assert_unique_ids(chapters)
    return chapters


def _assign_slug(title: str, used_ids: set[str]) -> str:
    """Derive a unique path-segment id from ``title``, de-duplicating against ``used_ids``.

    The first occurrence of a base slug keeps it verbatim; later collisions (case-insensitive) get
    ``-2``, ``-3`` … in reading order. ``used_ids`` is mutated to record the casefold of the result.
    """
    base = slugify_path_segment(title, fallback_prefix="chapter")
    candidate = base
    counter = 2
    while candidate.casefold() in used_ids:
        candidate = f"{base}-{counter}"
        counter += 1
    used_ids.add(candidate.casefold())
    return candidate


def _identity_fingerprint(kind: str, title: str, source_refs: list[str]) -> str:
    """Stable fingerprint of a chapter/group identity, used as the slug-registry key.

    Includes ``source_refs`` so two chapters that share a title (and thus the same base slug) still
    get distinct fingerprints — the registry can then pin each one's slug independently.
    """
    payload = "\x00".join([kind, unicodedata.normalize("NFC", title), *sorted(source_refs)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def compute_slug_remap(
    chapter_order: list[str],
    chapter_groups: dict[str, dict[str, object]],
    chapter_titles: dict[str, str],
    chapter_source_refs: dict[str, list[str]],
    registry: dict[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Map parse-time chapter/group ids to stable, registry-pinned slugs (zero churn).

    The parse stage already assigns each chapter/group a unique, deduplicated id from its title.
    This remaps those ids so that an identity already recorded in ``registry`` keeps its slug even
    when a newly added, same-base-slug chapter is inserted before it. Two passes:

    1. honour every existing registry pin whose slug is still free;
    2. assign the remaining (new) identities their parse-time id, de-duplicating only against pins.

    Because parse-time ids are already globally unique, pass 2 never collides two *new* entries, so
    entry order does not affect the outcome. Returns ``(remap, updated_registry)``; ``remap`` covers
    every chapter id and group id (``appendix`` maps to itself).
    """
    entries: list[tuple[str, str]] = []  # (parse_id, fingerprint), groups first then chapters
    seen: set[str] = set()
    for raw_gid, info in chapter_groups.items():
        gid = str(raw_gid)
        if gid in seen:
            continue
        seen.add(gid)
        gtitle = str((info or {}).get("title") or gid)
        entries.append((gid, _identity_fingerprint("group", gtitle, [])))
    for cid in chapter_order:
        if cid == APPENDIX_CHAPTER_ID or cid in seen:
            continue
        seen.add(cid)
        title = str(chapter_titles.get(cid, cid))
        refs = list(chapter_source_refs.get(cid, []))
        entries.append((cid, _identity_fingerprint("chapter", title, refs)))

    taken: set[str] = {APPENDIX_CHAPTER_ID.casefold()}
    remap: dict[str, str] = {APPENDIX_CHAPTER_ID: APPENDIX_CHAPTER_ID}
    updated: dict[str, str] = dict(registry)
    for parse_id, fingerprint in entries:  # pass 1: honour pins
        pinned = registry.get(fingerprint)
        if pinned and pinned.casefold() not in taken:
            remap[parse_id] = pinned
            taken.add(pinned.casefold())
    for parse_id, fingerprint in entries:  # pass 2: assign new identities
        if parse_id in remap:
            continue
        slug = parse_id
        counter = 2
        while slug.casefold() in taken:
            slug = f"{parse_id}-{counter}"
            counter += 1
        taken.add(slug.casefold())
        remap[parse_id] = slug
        updated[fingerprint] = slug
    return remap, updated


def chapter_groups_from_specs(specs: list[ChapterSpec]) -> dict[str, dict[str, object]]:
    """Project leaf specs into ``group_id -> {title, leaf_ids}`` preserving first-seen order."""
    groups: dict[str, dict[str, object]] = {}
    for spec in specs:
        if not spec.group_id:
            continue
        group = groups.setdefault(
            spec.group_id, {"title": spec.group_title or spec.group_id, "leaf_ids": []}
        )
        leaf_ids = group["leaf_ids"]
        if isinstance(leaf_ids, list):
            leaf_ids.append(spec.chapter_id)
    return groups


def _parse_leaf(item: dict[str, object], used_ids: set[str]) -> ChapterSpec:
    raw_title = str(item.get("title") or "").strip()
    if not raw_title:
        msg = "approved structure chapter entry must include a non-empty title"
        raise ValueError(msg)
    topics = _string_list(item.get("topics"))
    source_refs = _string_list(item.get("source_refs"))
    if not topics or not source_refs:
        msg = f"chapter {raw_title!r} must include non-empty topics and source_refs lists"
        raise ValueError(msg)
    chapter_id = _assign_slug(raw_title, used_ids)
    return ChapterSpec(
        chapter_id=chapter_id, title=raw_title, topics=topics, source_refs=source_refs
    )


def _parse_group(item: dict[str, object], index: int, used_ids: set[str]) -> list[ChapterSpec]:
    if item.get("topics") or item.get("source_refs"):
        msg = f"chapter group entry {index} must not mix 'sections' with 'topics'/'source_refs'"
        raise ValueError(msg)
    sections = item.get("sections")
    if not isinstance(sections, list) or not sections:
        msg = f"chapter group entry {index} 'sections' must be a non-empty list"
        raise ValueError(msg)
    group_title = str(item.get("title") or "").strip()
    if not group_title:
        msg = f"chapter group entry {index} must include a non-empty title"
        raise ValueError(msg)
    group_id = _assign_slug(group_title, used_ids)
    leaves: list[ChapterSpec] = []
    for sub_index, section in enumerate(sections, start=1):
        if not isinstance(section, dict):
            msg = f"section entry {sub_index} in group {group_id!r} must be a mapping"
            raise ValueError(msg)
        leaf_title = str(section.get("title") or "").strip()
        if not leaf_title:
            msg = f"section entry {sub_index} in group {group_id!r} must include a non-empty title"
            raise ValueError(msg)
        topics = _string_list(section.get("topics"))
        source_refs = _string_list(section.get("source_refs"))
        if not topics or not source_refs:
            msg = f"section {leaf_title!r} must include non-empty topics and source_refs lists"
            raise ValueError(msg)
        leaf_id = _assign_slug(leaf_title, used_ids)
        leaves.append(
            ChapterSpec(
                chapter_id=leaf_id,
                title=leaf_title,
                topics=topics,
                source_refs=source_refs,
                group_id=group_id,
                group_title=group_title,
            )
        )
    return leaves


def _assert_unique_ids(chapters: list[ChapterSpec]) -> None:
    seen: set[str] = set()
    for spec in chapters:
        if spec.chapter_id in seen:
            msg = f"duplicate chapter id {spec.chapter_id!r} in approved structure"
            raise ValueError(msg)
        seen.add(spec.chapter_id)


def extract_source_fragments(path: str | Path) -> list[SourceFragment]:
    source_path = Path(path)
    text = source_path.read_text(encoding="utf-8", errors="ignore")
    matches = list(SOURCE_REF_RE.finditer(text))
    source_id = source_path.stem
    if not matches:
        body = text.strip()
        if not body:
            return []
        return [
            SourceFragment(
                source_path=source_path.as_posix(),
                source_id=source_id,
                source_ref=f"{source_id}-text",
                body=body,
            )
        ]

    fragments: list[SourceFragment] = []
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[match.end() : next_start].strip()
        if not body:
            continue
        fragments.append(
            SourceFragment(
                source_path=source_path.as_posix(),
                source_id=source_id,
                source_ref=match.group(1),
                body=body,
            )
        )
    return fragments


def split_sources_by_structure(
    source_paths: list[str | Path], approved_structure: str
) -> SplitResult:
    specs = parse_approved_structure(approved_structure)
    fragments = [
        fragment
        for source_path in source_paths
        for fragment in extract_source_fragments(source_path)
    ]
    chapter_titles = {spec.chapter_id: spec.title for spec in specs}
    chapter_titles["appendix"] = "Appendix"
    chapter_fragments: dict[str, list[SourceFragment]] = {
        chapter_id: [] for chapter_id in chapter_titles
    }
    alignment: list[dict[str, object]] = []

    for fragment in fragments:
        chapter_id, confidence, reason = _assign_fragment(fragment, specs)
        chapter_fragments.setdefault(chapter_id, []).append(fragment)
        alignment.append(
            {
                "source_path": fragment.source_path,
                "source_id": fragment.source_id,
                "source_ref": fragment.source_ref,
                "chapter_id": chapter_id,
                "confidence": confidence,
                "reason": reason,
                "chars": len(fragment.body),
            }
        )

    chapters = {
        chapter_id: _render_chapter_source(chapter_id, chapter_titles[chapter_id], assigned)
        for chapter_id, assigned in chapter_fragments.items()
        if assigned or chapter_id != "appendix"
    }
    assigned_count = sum(1 for item in alignment if item["chapter_id"] != "appendix")
    total_count = len(alignment)
    coverage = {
        "total_fragments": total_count,
        "assigned_fragments": assigned_count,
        "unassigned_fragments": total_count - assigned_count,
        "assigned_ratio": round(assigned_count / total_count, 4) if total_count else 1.0,
    }
    report_md = _render_report(specs, alignment, coverage)
    return SplitResult(
        chapters,
        chapter_titles,
        alignment,
        coverage,
        report_md,
        chapter_groups=chapter_groups_from_specs(specs),
        chapter_order=list(chapters.keys()),
        chapter_source_refs={spec.chapter_id: list(spec.source_refs) for spec in specs},
    )


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    items: list[str] = []
    for item in value:
        text = str(item).strip().strip("` ")
        if text:
            items.append(text)
    return items


def _assign_fragment(fragment: SourceFragment, specs: list[ChapterSpec]) -> tuple[str, float, str]:
    for spec in specs:
        if any(_ref_matches(fragment.source_ref, pattern) for pattern in spec.source_refs):
            return spec.chapter_id, 1.0, "source_ref"

    best: tuple[str, float, str] | None = None
    text = fragment.body.lower()
    for spec in specs:
        terms = _keywords(" ".join([spec.title, *spec.topics]))
        matches = sum(1 for term in terms if term in text)
        if matches == 0:
            continue
        confidence = min(0.85, 0.35 + matches * 0.1)
        if best is None or confidence > best[1]:
            best = (spec.chapter_id, confidence, "keyword")
    return best or ("appendix", 0.0, "unassigned")


def _ref_matches(source_ref: str, pattern: str) -> bool:
    pattern = pattern.strip()
    if source_ref == pattern:
        return True
    if ".." not in pattern:
        return False

    start, end = (part.strip() for part in pattern.split("..", 1))
    parsed_ref = _split_number_suffix(source_ref)
    parsed_start = _split_number_suffix(start)
    parsed_end = _split_number_suffix(end)
    if not parsed_ref or not parsed_start or not parsed_end:
        return False
    ref_prefix, ref_number = parsed_ref
    start_prefix, start_number = parsed_start
    end_prefix, end_number = parsed_end
    if ref_prefix != start_prefix or ref_prefix != end_prefix:
        return False
    return start_number <= ref_number <= end_number


def _split_number_suffix(value: str) -> tuple[str, int] | None:
    match = re.match(r"^(.*?)(\d+)$", value)
    if not match:
        return None
    return match.group(1), int(match.group(2))


def _keywords(text: str) -> set[str]:
    stopwords = {"and", "the", "with", "from", "chapter", "search"}
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", text.lower())
    return {word for word in words if word not in stopwords}


def _render_chapter_source(chapter_id: str, title: str, fragments: list[SourceFragment]) -> str:
    blocks = [f"# {_display_chapter_heading(chapter_id, title)}"]
    for fragment in fragments:
        blocks.append(
            f"<!-- source_ref: {fragment.source_ref} -->\n\n"
            f"{fragment.body.strip()}\n\n"
            f"<!-- source_path: {fragment.source_path} -->"
        )
    return "\n\n".join(blocks).strip() + "\n"


def _display_chapter_heading(chapter_id: str, title: str) -> str:
    # The title is the verbatim free-form chapter name (the id is just a slug derived from it),
    # so the source H1 is the title as-is — no "Chapter N" prefix is synthesised any more.
    return title.strip() or chapter_id


def _render_report(
    specs: list[ChapterSpec], alignment: list[dict[str, object]], coverage: dict[str, float | int]
) -> str:
    chapter_ids = [spec.chapter_id for spec in specs] + ["appendix"]
    source_ids = sorted({str(item["source_id"]) for item in alignment})
    lines = [
        "# Chapter Split Report",
        "",
        f"- total fragments: {coverage['total_fragments']}",
        f"- assigned fragments: {coverage['assigned_fragments']}",
        f"- unassigned fragments: {coverage['unassigned_fragments']}",
        f"- assigned ratio: {coverage['assigned_ratio']}",
        "",
        "| source | " + " | ".join(chapter_ids) + " |",
        "|---|" + "|".join("---" for _ in chapter_ids) + "|",
    ]
    for source_id in source_ids:
        counts = [
            sum(
                1
                for item in alignment
                if item["source_id"] == source_id and item["chapter_id"] == chapter_id
            )
            for chapter_id in chapter_ids
        ]
        lines.append(f"| {source_id} | " + " | ".join(str(count) for count in counts) + " |")

    unassigned = [item for item in alignment if item["chapter_id"] == "appendix"]
    lines.extend(["", "## Unassigned"])
    if unassigned:
        lines.extend(f"- `{item['source_ref']}` from `{item['source_id']}`" for item in unassigned)
    else:
        lines.append("- none")
    return "\n".join(lines) + "\n"
