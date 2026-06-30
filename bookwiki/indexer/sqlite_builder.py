from __future__ import annotations

import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from bookwiki.indexer import embedder
from bookwiki.indexer.mdx_parser import MdxPage, parse_mdx_file
from bookwiki.indexer.rag_chunker import RagChunk, chunk_page
from bookwiki.utils.files import ensure_dir


def build_sqlite_index(
    content_dir: str | Path,
    db_path: str | Path,
    *,
    embed: bool = False,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> Path:
    content_dir = Path(content_dir)
    db_path = Path(db_path)
    ensure_dir(db_path.parent)
    tmp_path = db_path.with_suffix(f"{db_path.suffix}.tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    pages = [parse_mdx_file(path, root=content_dir) for path in sorted(content_dir.rglob("*.mdx"))]

    conn = sqlite3.connect(tmp_path)
    try:
        _create_schema(conn)
        _insert_pages(conn, pages)
        _insert_chunks(conn, pages)
        _insert_learning_items(conn, pages)
        _insert_source_refs(conn, pages)
        if embed:
            resolved_model = model or embedder.DEFAULT_EMBED_MODEL
            if not api_key:
                raise RuntimeError("embedding 需要 api_key(OPENROUTER_API_KEY)")
            _insert_embeddings(
                conn,
                resolved_model,
                api_key,
                base_url or "https://openrouter.ai/api/v1",
            )
        conn.execute("INSERT INTO fts_chunks(fts_chunks) VALUES('rebuild')")
        conn.commit()
    finally:
        conn.close()

    try:
        os.replace(tmp_path, db_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()
    return db_path


def rebuild_sqlite(book_dir: str | Path) -> Path:
    book_dir = Path(book_dir)
    return build_sqlite_index(
        book_dir / "site" / "content" / "docs",
        book_dir / "site" / ".bookwiki" / "bookwiki.sqlite",
    )


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE pages
        (
            id               TEXT PRIMARY KEY,
            slug             TEXT NOT NULL UNIQUE,
            path             TEXT NOT NULL,
            title            TEXT NOT NULL,
            type             TEXT NOT NULL,
            chapter_id       TEXT,
            order_index      INTEGER,
            frontmatter_json TEXT NOT NULL
        );

        CREATE TABLE chunks
        (
            "rowid"          INTEGER PRIMARY KEY AUTOINCREMENT,
            chunk_id         TEXT    NOT NULL UNIQUE,
            page_id          TEXT    NOT NULL,
            chapter_id       TEXT,
            section_id       TEXT,
            chunk_index      INTEGER NOT NULL,
            heading_path     TEXT,
            text             TEXT    NOT NULL,
            source_refs_json TEXT    NOT NULL,
            token_count      INTEGER,
            embedding        BLOB,
            FOREIGN KEY (page_id) REFERENCES pages (id)
        );

        CREATE VIRTUAL TABLE fts_chunks USING fts5(
            text,
            heading_path,
            content='chunks',
            content_rowid='rowid',
            tokenize='trigram'
        );

        CREATE TABLE quiz_items
        (
            id               TEXT PRIMARY KEY,
            chapter_id       TEXT NOT NULL,
            page_id          TEXT NOT NULL,
            type             TEXT NOT NULL,
            difficulty       TEXT,
            concepts_json    TEXT NOT NULL,
            question         TEXT NOT NULL,
            options_json     TEXT,
            answer           TEXT NOT NULL,
            explanation      TEXT,
            grading_json     TEXT,
            from_exam        INTEGER NOT NULL DEFAULT 0,
            source_refs_json TEXT NOT NULL
        );

        CREATE TABLE card_items
        (
            id               TEXT PRIMARY KEY,
            chapter_id       TEXT NOT NULL,
            page_id          TEXT NOT NULL,
            front            TEXT NOT NULL,
            back             TEXT NOT NULL,
            tags_json        TEXT NOT NULL,
            source_refs_json TEXT NOT NULL
        );

        CREATE TABLE source_refs
        (
            id        TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            label     TEXT NOT NULL,
            page      INTEGER,
            slide     INTEGER,
            path      TEXT
        );

        CREATE VIEW documents AS
        SELECT
            pages.rowid AS id,
            pages.path AS path,
            pages.title AS title,
            COALESCE(
                (
                    SELECT group_concat(chunks.text, char(10) || char(10))
                    FROM chunks
                    WHERE chunks.page_id = pages.id
                ),
                ''
            ) AS body
        FROM pages;

        CREATE TABLE search_meta
        (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )


def _insert_pages(conn: sqlite3.Connection, pages: list[MdxPage]) -> None:
    for index, page in enumerate(pages):
        order_index = page.order_index if page.order_index is not None else index
        conn.execute(
            """
            INSERT INTO pages
                (id, slug, path, title, type, chapter_id, order_index, frontmatter_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                page.id,
                page.slug,
                page.relative_path,
                page.title,
                page.type,
                page.chapter_id,
                order_index,
                _json(page.frontmatter),
            ),
        )


def _insert_chunks(conn: sqlite3.Connection, pages: list[MdxPage]) -> None:
    for page in pages:
        for chunk in chunk_page(page):
            _insert_chunk(conn, chunk)


def _insert_chunk(conn: sqlite3.Connection, chunk: RagChunk) -> None:
    conn.execute(
        """
        INSERT INTO chunks
            (
                chunk_id,
                page_id,
                chapter_id,
                section_id,
                chunk_index,
                heading_path,
                text,
                source_refs_json,
                token_count
            )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            chunk.chunk_id,
            chunk.page_id,
            chunk.chapter_id,
            chunk.section_id,
            chunk.chunk_index,
            chunk.heading_path,
            chunk.text,
            _json(chunk.source_refs),
            chunk.token_count,
        ),
    )


def _insert_learning_items(conn: sqlite3.Connection, pages: list[MdxPage]) -> None:
    for page in pages:
        chapter_id = page.chapter_id or page.id
        concepts = page.frontmatter.get("concepts")
        page_concepts = concepts if isinstance(concepts, list) else []
        quiz_ids: set[str] = set()
        for index, item in enumerate(page.quiz_items, start=1):
            source_refs = _item_source_refs(item)
            options = item.get("choices", item.get("options"))
            conn.execute(
                """
                INSERT INTO quiz_items
                    (
                        id,
                        chapter_id,
                        page_id,
                        type,
                        difficulty,
                        concepts_json,
                        question,
                        options_json,
                        answer,
                        explanation,
                        grading_json,
                        from_exam,
                        source_refs_json
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _unique_item_id(page, item, "quiz", index, quiz_ids),
                    chapter_id,
                    page.id,
                    str(item.get("type") or "multiple_choice"),
                    _optional_str(item.get("difficulty")),
                    _json(item.get("concepts") or page_concepts),
                    str(item.get("question") or ""),
                    _json(options) if options is not None else None,
                    str(item.get("answer") or ""),
                    _optional_str(item.get("explanation")),
                    (
                        _json(item.get("grading_json"))
                        if item.get("grading_json") is not None
                        else None
                    ),
                    1 if item.get("from_exam") else 0,
                    _json(source_refs),
                ),
            )
        card_ids: set[str] = set()
        for index, item in enumerate(page.card_items, start=1):
            source_refs = _item_source_refs(item)
            conn.execute(
                """
                INSERT INTO card_items
                    (id, chapter_id, page_id, front, back, tags_json, source_refs_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _unique_item_id(page, item, "card", index, card_ids),
                    chapter_id,
                    page.id,
                    str(item.get("front") or ""),
                    str(item.get("back") or ""),
                    _json(item.get("tags") or []),
                    _json(source_refs),
                ),
            )


def _insert_source_refs(conn: sqlite3.Connection, pages: list[MdxPage]) -> None:
    refs: dict[str, str] = {}
    for page in pages:
        for ref_id in page.source_refs:
            refs.setdefault(ref_id, page.relative_path)
    for ref_id, path in sorted(refs.items()):
        source_id, page_number, slide_number = _source_ref_parts(ref_id)
        conn.execute(
            """
            INSERT INTO source_refs (id, source_id, label, page, slide, path)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (ref_id, source_id, ref_id, page_number, slide_number, path),
        )


def _item_source_refs(item: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for value in item.get("source_refs") or []:
        if isinstance(value, str) and value.strip() and value.strip() not in refs:
            refs.append(value.strip())
    citations = item.get("citations") or []
    if isinstance(citations, list):
        for citation in citations:
            if isinstance(citation, dict):
                ref_id = citation.get("ref_id")
                if isinstance(ref_id, str) and ref_id.strip() and ref_id.strip() not in refs:
                    refs.append(ref_id.strip())
    return refs


def _source_ref_parts(ref_id: str) -> tuple[str, int | None, int | None]:
    page_match = re.search(r"^(?P<source>.+?)-p(?P<page>\d+)$", ref_id)
    if page_match:
        return page_match.group("source"), int(page_match.group("page")), None
    slide_match = re.search(r"^(?P<source>.+?)-slide(?P<slide>\d+)$", ref_id)
    if slide_match:
        return slide_match.group("source"), None, int(slide_match.group("slide"))
    if "." in ref_id:
        return ref_id.split(".", 1)[0], None, None
    if "-" in ref_id:
        return ref_id.split("-", 1)[0], None, None
    return ref_id, None, None


def _item_id(page: MdxPage, item: dict[str, Any], prefix: str, index: int) -> str:
    raw = item.get("id")
    suffix = str(raw).strip() if raw is not None and str(raw).strip() else f"{prefix}-{index:03d}"
    return f"{page.id}:{suffix}"


def _unique_item_id(
    page: MdxPage,
    item: dict[str, Any],
    prefix: str,
    index: int,
    used_ids: set[str],
) -> str:
    base_id = _item_id(page, item, prefix, index)
    if base_id not in used_ids:
        used_ids.add(base_id)
        return base_id

    candidate = f"{base_id}-{index:03d}"
    counter = 2
    while candidate in used_ids:
        candidate = f"{base_id}-{index:03d}-{counter}"
        counter += 1
    used_ids.add(candidate)
    return candidate


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _insert_embeddings(
    conn: sqlite3.Connection, model: str, api_key: str, base_url: str
) -> None:
    rows = conn.execute("SELECT rowid, text FROM chunks ORDER BY rowid").fetchall()
    if not rows:
        return
    texts = [row[1] for row in rows]
    vectors = embedder.embed_texts(texts, model=model, api_key=api_key, base_url=base_url)
    dim = len(vectors[0]) if vectors else embedder.DEFAULT_EMBED_DIM
    for (rowid, _text), vec in zip(rows, vectors):
        conn.execute(
            "UPDATE chunks SET embedding = ? WHERE rowid = ?",
            (embedder.floats_to_blob(vec), rowid),
        )
    conn.execute(
        "INSERT OR REPLACE INTO search_meta (key, value) VALUES ('embedding_model', ?)",
        (model,),
    )
    conn.execute(
        "INSERT OR REPLACE INTO search_meta (key, value) VALUES ('embedding_dim', ?)",
        (str(dim),),
    )


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None and str(value).strip() else None
