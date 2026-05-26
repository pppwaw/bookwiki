from __future__ import annotations

import re
from dataclasses import dataclass, field

from bookwiki.indexer.mdx_parser import MdxPage, source_refs_from_text


@dataclass(frozen=True)
class RagChunk:
    chunk_id: str
    page_id: str
    chapter_id: str | None
    section_id: str | None
    chunk_index: int
    heading_path: str | None
    text: str
    source_refs: list[str] = field(default_factory=list)
    token_count: int | None = None


def chunk_page(page: MdxPage, max_chars: int = 1200) -> list[RagChunk]:
    sections = _heading_sections(page.body)
    chunks: list[RagChunk] = []
    summary = str(page.frontmatter.get("summary") or "").strip()
    if summary:
        chunks.append(
            RagChunk(
                chunk_id=f"{page.id}#chunk-001",
                page_id=page.id,
                chapter_id=page.chapter_id,
                section_id="summary",
                chunk_index=0,
                heading_path=f"{page.title} > Summary",
                text=summary,
                source_refs=list(page.source_refs),
                token_count=max(1, len(summary) // 4),
            )
        )
    for heading_path, section_id, raw in sections:
        text = _plain_text(raw)
        if not text:
            continue
        refs = source_refs_from_text(raw)
        for part in _split_text(text, max_chars=max_chars):
            chunk_index = len(chunks)
            part_section_id = section_id
            if len(_split_text(text, max_chars=max_chars)) > 1:
                part_section_id = f"{section_id}-{chunk_index + 1:02d}" if section_id else None
            chunks.append(
                RagChunk(
                    chunk_id=f"{page.id}#chunk-{chunk_index + 1:03d}",
                    page_id=page.id,
                    chapter_id=page.chapter_id,
                    section_id=part_section_id,
                    chunk_index=chunk_index,
                    heading_path=heading_path,
                    text=part,
                    source_refs=refs,
                    token_count=max(1, len(part) // 4),
                )
            )
    if not chunks:
        text = _plain_text(page.body)
        chunks.append(
            RagChunk(
                chunk_id=f"{page.id}#chunk-001",
                page_id=page.id,
                chapter_id=page.chapter_id,
                section_id=None,
                chunk_index=0,
                heading_path=page.title,
                text=text,
                source_refs=page.source_refs,
                token_count=max(1, len(text) // 4),
            )
        )
    return chunks


def chunk_markdown(markdown: str, limit: int = 1200) -> list[str]:
    return [markdown[index : index + limit] for index in range(0, len(markdown), limit)] or [""]


def _heading_sections(body: str) -> list[tuple[str | None, str | None, str]]:
    matches = list(re.finditer(r"^(#{1,6})\s+(.+?)\s*$", body, flags=re.MULTILINE))
    if not matches:
        return [(None, None, body)]

    sections: list[tuple[str | None, str | None, str]] = []
    stack: list[str] = []
    prefix = body[: matches[0].start()].strip()
    if prefix:
        sections.append((None, "intro", prefix))
    for index, match in enumerate(matches):
        level = len(match.group(1))
        title = match.group(2).strip()
        stack = stack[: level - 1]
        stack.append(title)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        raw = body[match.start() : end].strip()
        sections.append((" > ".join(stack), _slug(title), raw))
    return sections


def _plain_text(text: str) -> str:
    text = re.sub(r"<(QuizBlock|AnkiDeck)\b[\s\S]*?</\1>", "", text)
    text = re.sub(r"<(QuizBlock|AnkiDeck)\b[^>]*/>\s*", "", text, flags=re.DOTALL)
    text = re.sub(r"<SourceRef\b[^>]*>\s*", "", text)
    text = re.sub(r"<!--\s*source_ref:\s*[^>]+-->", "", text)
    text = re.sub(r"^\s*---\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _split_text(text: str, max_chars: int) -> list[str]:
    if max_chars <= 0 or len(text) <= max_chars:
        return [text]
    paragraphs = re.split(r"\n\s*\n", text)
    parts: list[str] = []
    current = ""
    for paragraph in paragraphs:
        candidate = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            parts.append(current)
        if len(paragraph) <= max_chars:
            current = paragraph
        else:
            parts.extend(
                paragraph[index : index + max_chars]
                for index in range(0, len(paragraph), max_chars)
            )
            current = ""
    if current:
        parts.append(current)
    return [part.strip() for part in parts if part.strip()]


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "section"
