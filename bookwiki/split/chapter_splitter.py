from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from bookwiki.convert.common import SOURCE_REF_RE


@dataclass(frozen=True)
class ChapterSpec:
    chapter_id: str
    title: str
    goal: str = ""
    scope: str = ""
    source_refs: list[str] = field(default_factory=list)


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


def parse_approved_structure(markdown: str) -> list[ChapterSpec]:
    headings = [
        (match, parsed)
        for match in re.finditer(r"^##\s+(.+?)\s*$", markdown, flags=re.MULTILINE)
        if (parsed := _parse_chapter_heading(match.group(1)))
    ]
    chapters: list[ChapterSpec] = []
    for index, (match, (chapter_id, title)) in enumerate(headings):
        start = match.end()
        end = headings[index + 1][0].start() if index + 1 < len(headings) else len(markdown)
        block = markdown[start:end]
        chapters.append(
            ChapterSpec(
                chapter_id=chapter_id,
                title=title,
                goal=_extract_field(block, "目标", "goal"),
                scope=_extract_field(block, "范围", "scope"),
                source_refs=_extract_source_refs(block),
            )
        )
    return chapters or [
        ChapterSpec("ch01", "Foundations", source_refs=[]),
        ChapterSpec("ch02", "Practice", source_refs=[]),
    ]


def _parse_chapter_heading(heading: str) -> tuple[str, str] | None:
    heading = heading.strip()
    legacy = re.match(r"^(ch\d+)\s+(.+?)\s*$", heading, flags=re.IGNORECASE)
    if legacy:
        return legacy.group(1).lower(), legacy.group(2).strip()
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


def split_sources_by_structure(source_paths: list[str | Path], approved_md: str) -> SplitResult:
    specs = parse_approved_structure(approved_md)
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
    return SplitResult(chapters, chapter_titles, alignment, coverage, report_md)


def _extract_field(block: str, *names: str) -> str:
    for name in names:
        match = re.search(rf"^\s*-\s*{re.escape(name)}\s*[:：]\s*(.+)$", block, re.MULTILINE)
        if match:
            return match.group(1).strip()
    return ""


def _extract_source_refs(block: str) -> list[str]:
    refs: list[str] = []
    in_sources = False
    for line in block.splitlines():
        stripped = line.strip()
        if re.match(r"^-\s*(来源|sources?)\s*[:：]\s*$", stripped, flags=re.IGNORECASE):
            in_sources = True
            continue
        if in_sources:
            item = re.match(r"^-\s+(.+)$", stripped)
            if item:
                refs.append(item.group(1).strip("` "))
                continue
            if stripped and not line.startswith((" ", "\t")):
                in_sources = False
        inline = re.match(r"^-\s*(source_ref|来源)\s*[:：]\s*(.+)$", stripped, flags=re.IGNORECASE)
        if inline:
            refs.append(inline.group(2).strip("` "))
    return refs


def _assign_fragment(fragment: SourceFragment, specs: list[ChapterSpec]) -> tuple[str, float, str]:
    for spec in specs:
        if any(_ref_matches(fragment.source_ref, pattern) for pattern in spec.source_refs):
            return spec.chapter_id, 1.0, "source_ref"

    best: tuple[str, float, str] | None = None
    text = fragment.body.lower()
    for spec in specs:
        terms = _keywords(f"{spec.title} {spec.goal} {spec.scope}")
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


def _render_chapter_source(
    chapter_id: str, title: str, fragments: list[SourceFragment]
) -> str:
    blocks = [f"# {_display_chapter_heading(chapter_id, title)}"]
    for fragment in fragments:
        blocks.append(
            f"<!-- source_ref: {fragment.source_ref} -->\n\n"
            f"{fragment.body.strip()}\n\n"
            f"<!-- source_path: {fragment.source_path} -->"
        )
    return "\n\n".join(blocks).strip() + "\n"


def _display_chapter_heading(chapter_id: str, title: str) -> str:
    chapter = re.match(r"^chapter-(\d+)$", chapter_id)
    if chapter:
        return f"Chapter {int(chapter.group(1))} {title}"
    return f"{chapter_id} {title}"


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
