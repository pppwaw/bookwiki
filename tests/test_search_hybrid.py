from __future__ import annotations

import math
import sqlite3
import struct
from pathlib import Path

from bookwiki.indexer import embedder
from bookwiki.indexer.sqlite_builder import build_sqlite_index


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
