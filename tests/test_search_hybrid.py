from __future__ import annotations

import math
import sqlite3
import struct
from pathlib import Path

from bookwiki.indexer import embedder
from bookwiki.indexer.mdx_parser import parse_mdx_file
from bookwiki.indexer.rag_chunker import chunk_page
from bookwiki.indexer.sqlite_builder import build_sqlite_index


def _page(tmp_path: Path, body: str, *, summary: str | None = None, title: str = "反向传播"):
    front = f"---\ntitle: {title}\ntype: chapter\nchapter_id: ch01\n"
    if summary is not None:
        front += f"summary: {summary}\n"
    front += "---\n"
    path = tmp_path / "content" / "docs" / "chapters" / "ch01.mdx"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(front + body, encoding="utf-8")
    return parse_mdx_file(path, root=tmp_path / "content" / "docs")


def _write_page(root: Path) -> None:
    page = root / "content" / "docs" / "chapters" / "ch01.mdx"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\ntitle: 反向传播\ntype: chapter\nchapter_id: ch01\n---\n"
        "# 反向传播\n\n反向传播是一种用于训练神经网络的算法。\n",
        encoding="utf-8",
    )


def test_trigram_enables_chinese_substring_match(tmp_path: Path) -> None:
    _write_page(tmp_path)
    db = build_sqlite_index(tmp_path / "content" / "docs", tmp_path / "out.sqlite")
    conn = sqlite3.connect(db)
    try:
        hits = conn.execute(
            "SELECT count(*) FROM fts_chunks WHERE fts_chunks MATCH ?", ('"反向传播"',)
        ).fetchone()[0]
    finally:
        conn.close()
    assert hits >= 1


def test_schema_has_embedding_column_and_meta_table(tmp_path: Path) -> None:
    _write_page(tmp_path)
    db = build_sqlite_index(tmp_path / "content" / "docs", tmp_path / "out.sqlite")
    conn = sqlite3.connect(db)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
        assert "embedding" in cols
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "search_meta" in tables
    finally:
        conn.close()


def test_chunk_strips_mdx_components_keeps_math(tmp_path: Path) -> None:
    body = (
        "# 反向传播\n\n"
        '反向传播用于训练。<PreviewLink href={"/docs/x"} summary={"摘要文本"}>相关页</PreviewLink>\n\n'
        "$$\n\\iint_S \\frac{\\partial N}{\\partial x} dA\n$$\n\n"
        "其中 $C$ 取正向。\n"
    )
    chunks = chunk_page(_page(tmp_path, body))
    joined = "\n".join(c.text for c in chunks)
    assert "PreviewLink" not in joined
    assert "summary={" not in joined
    assert "相关页" in joined  # 组件子文本保留
    assert "\\iint" in joined  # 公式保留给 LLM
    assert "$$" in joined


def test_chunk_skips_referenced_by(tmp_path: Path) -> None:
    body = (
        "# 概念\n\n正文段落内容。\n\n"
        "## Referenced By\n\n"
        '- <PreviewLink href={"/docs/x"} summary={"s"}>14.4 标题</PreviewLink>\n'
    )
    chunks = chunk_page(_page(tmp_path, body))
    assert all((c.heading_path or "").split(" > ")[-1].strip().casefold() != "referenced by" for c in chunks)
    assert all("Referenced By" not in c.text for c in chunks)
    assert any("正文段落内容" in c.text for c in chunks)


def test_summary_is_chunked_and_searchable(tmp_path: Path) -> None:
    chunks = chunk_page(_page(tmp_path, "# 标题\n\n正文。\n", summary="这是页面摘要"))
    summary_chunks = [c for c in chunks if c.section_id == "summary"]
    assert len(summary_chunks) == 1
    assert summary_chunks[0].text == "这是页面摘要"


def test_floats_to_blob_roundtrip() -> None:
    blob = embedder.floats_to_blob([1.0, 2.0, 3.0])
    assert len(blob) == 12
    assert list(struct.unpack("<3f", blob)) == [1.0, 2.0, 3.0]


def test_embed_texts_normalizes(monkeypatch) -> None:
    def fake_post(texts, *, model, api_key, base_url):
        return [[3.0, 4.0] for _ in texts], 7  # 未归一化(模长 5)+ token 数

    monkeypatch.setattr(embedder, "_raw_embed", fake_post)
    out, tokens = embedder.embed_texts(["a", "b"], model="m", api_key="k", base_url="u")
    assert len(out) == 2
    assert tokens == 7
    assert math.isclose(math.hypot(*out[0]), 1.0, rel_tol=1e-6)


def test_embed_cost_cny_uses_usd_to_cny() -> None:
    from bookwiki.scheduler.llm import OPENROUTER_USD_TO_CNY

    cost = embedder.embed_cost_cny(1_000_000)
    assert math.isclose(cost, embedder.EMBED_PRICE_USD_PER_1M * OPENROUTER_USD_TO_CNY, rel_tol=1e-9)


def test_build_writes_embeddings_and_meta(tmp_path: Path, monkeypatch) -> None:
    from bookwiki.indexer import sqlite_builder

    def fake_embed(texts, *, model, api_key, base_url):
        return [[1.0] + [0.0] * 1023 for _ in texts], 42

    monkeypatch.setattr(sqlite_builder.embedder, "embed_texts", fake_embed)
    _write_page(tmp_path)
    db = sqlite_builder.build_sqlite_index(
        tmp_path / "content" / "docs",
        tmp_path / "out.sqlite",
        embed=True,
        model="m",
        api_key="k",
        base_url="u",
    )
    conn = sqlite3.connect(db)
    try:
        blob, dim = conn.execute(
            "SELECT embedding, length(embedding) FROM chunks LIMIT 1"
        ).fetchone()
        assert blob is not None
        assert dim == 1024 * 4  # float32
        meta = dict(conn.execute("SELECT key, value FROM search_meta"))
        assert meta["embedding_model"] == "m"
        assert meta["embedding_dim"] == "1024"
    finally:
        conn.close()
