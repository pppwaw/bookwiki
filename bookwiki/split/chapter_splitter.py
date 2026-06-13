from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from bookwiki.convert.common import SOURCE_REF_RE


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


def parse_approved_structure(structure_yaml: str) -> list[ChapterSpec]:
    """Parse the approved structure YAML into a flat, depth-first list of leaf chapters.

    Two shapes are accepted per top-level ``chapters`` entry:

    * **Flat leaf** (backward compatible): ``{title: "Chapter 6 ...", topics, source_refs}``.
    * **Group** (two-level): ``{title: "Chapter 9 ...", sections: [<section>, ...]}`` where each
      section is ``{title: "9.2 ...", topics, source_refs}``. Sections are flattened into leaf
      ``ChapterSpec``s carrying ``group_id``/``group_title``; reading order is preserved.

    A group entry must not also carry ``topics``/``source_refs``, and every section's leading
    chapter number must match the group's number.
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
    for index, item in enumerate(payload["chapters"], start=1):
        if not isinstance(item, dict):
            msg = f"chapter entry {index} must be a mapping"
            raise ValueError(msg)
        if item.get("sections") is not None:
            chapters.extend(_parse_group(item, index))
        else:
            chapters.append(_parse_leaf(item))
    if not chapters:
        msg = "approved structure must contain at least one chapter"
        raise ValueError(msg)
    _assert_unique_ids(chapters)
    return chapters


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


def _parse_leaf(item: dict[str, object]) -> ChapterSpec:
    parsed = _parse_chapter_heading(str(item.get("title") or ""))
    if not parsed:
        msg = "approved structure chapter titles must look like 'Chapter 6 Point Estimation'"
        raise ValueError(msg)
    chapter_id, title = parsed
    topics = _string_list(item.get("topics"))
    source_refs = _string_list(item.get("source_refs"))
    if not topics or not source_refs:
        msg = f"chapter {chapter_id!r} must include non-empty topics and source_refs lists"
        raise ValueError(msg)
    return ChapterSpec(chapter_id=chapter_id, title=title, topics=topics, source_refs=source_refs)


def _parse_group(item: dict[str, object], index: int) -> list[ChapterSpec]:
    if item.get("topics") or item.get("source_refs"):
        msg = f"chapter group entry {index} must not mix 'sections' with 'topics'/'source_refs'"
        raise ValueError(msg)
    sections = item.get("sections")
    if not isinstance(sections, list) or not sections:
        msg = f"chapter group entry {index} 'sections' must be a non-empty list"
        raise ValueError(msg)
    group = _parse_chapter_heading(str(item.get("title") or ""))
    if not group:
        msg = "approved structure group titles must look like 'Chapter 9 Infinite Series'"
        raise ValueError(msg)
    group_id, _ = group
    group_number = int(group_id.split("-", 1)[1])
    # Use the raw heading as the display title so a bare "Chapter 9" is not duplicated into
    # "Chapter 9 Chapter 9"; a descriptive "Chapter 9 Infinite Series" is kept verbatim.
    group_title = str(item.get("title") or "").strip()
    leaves: list[ChapterSpec] = []
    for sub_index, section in enumerate(sections, start=1):
        if not isinstance(section, dict):
            msg = f"section entry {sub_index} in group {group_id!r} must be a mapping"
            raise ValueError(msg)
        parsed = _parse_section_heading(str(section.get("title") or ""))
        if not parsed:
            msg = (
                f"section titles in group {group_id!r} must start with a number "
                "like '9.2 Infinite Series'"
            )
            raise ValueError(msg)
        leaf_id, leaf_number, leaf_title = parsed
        if leaf_number != group_number:
            msg = (
                f"section {leaf_title!r} (chapter {leaf_number}) does not belong to "
                f"group 'Chapter {group_number}'"
            )
            raise ValueError(msg)
        topics = _string_list(section.get("topics"))
        source_refs = _string_list(section.get("source_refs"))
        if not topics or not source_refs:
            msg = f"section {leaf_id!r} must include non-empty topics and source_refs lists"
            raise ValueError(msg)
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


def _parse_chapter_heading(heading: str) -> tuple[str, str] | None:
    heading = heading.strip()
    chapter = re.match(
        r"^chapter\s+(\d+)\b\s*(?::|-)?\s*(.*?)\s*$",
        heading,
        flags=re.IGNORECASE,
    )
    if chapter:
        number = int(chapter.group(1))
        title = chapter.group(2).strip() or f"Chapter {number}"
        return f"chapter-{number}", title
    return None


def _parse_section_heading(heading: str) -> tuple[str, int, str] | None:
    """Parse a section title like ``9.2 Infinite Series`` or ``11.1-11.4 Vectors``.

    Returns ``(leaf_id, group_number, full_title)`` where ``leaf_id`` normalises the leading
    numeric token (dots/dashes -> ``-``) into ``chapter-9-2``, and ``group_number`` is the first
    integer (the owning chapter). The full original title is kept verbatim for display.
    """
    heading = heading.strip()
    match = re.match(r"^(\d+(?:[.\-]\d+)*)\b", heading)
    if not match:
        return None
    number_token = match.group(1)
    leaf_id = "chapter-" + re.sub(r"[.\-]+", "-", number_token)
    group_number = int(re.match(r"^(\d+)", number_token).group(1))
    return leaf_id, group_number, heading


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
    clean = title.strip()
    # Section/leaf titles already start with their number (e.g. "9.2 Infinite Series");
    # keep verbatim. Likewise an explicit "Chapter N ..." title is already display-ready.
    if re.match(r"^(chapter\s+\d+\b|\d)", clean, flags=re.IGNORECASE):
        return clean
    chapter = re.fullmatch(r"chapter-(\d+)", chapter_id)
    if chapter:
        return f"Chapter {int(chapter.group(1))} {clean}".strip()
    return f"{chapter_id} {clean}".strip() if clean else chapter_id


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
