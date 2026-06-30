# 统一中文搜索 + 轻量语义混合检索 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把文档搜索框与 RAG 聊天统一到一套 Chinese-aware 的 SQLite 检索:trigram 关键词 + bge-m3 语义,RRF 融合。

**Architecture:** 构建期(Python)给每个 chunk 用 OpenRouter `baai/bge-m3` 算向量存进 SQLite,FTS5 改 trigram;运行期(Node/Vercel)给 query 也用 OpenRouter 算向量,对全库 BLOB 暴力 cosine,与 trigram 结果用 RRF 融合。文档框 `/api/search` 与聊天 `search_book` 共用 `searchChunks`。

**Tech Stack:** Python(sqlite3、litellm/httpx、pytest)、TypeScript(better-sqlite3、fumadocs-core、vitest)、OpenRouter embeddings(OpenAI 兼容)。

## Global Constraints

- 向量模型默认 `baai/bge-m3`,1024 维;可经 `BOOKWIKI_EMBED_MODEL` / `BOOKWIKI_EMBED_DIM` 切换。
- 向量存储:`chunks.embedding` 为 float32 little-endian BLOB,L2 normalized;cosine 用点积。
- FTS5 分词器:`trigram`(≥3 字 MATCH,<3 字走 LIKE 回退)。
- 融合:RRF,`k=60`。
- 不引入向量库/SQLite 原生扩展/客户端或服务端常驻 embedding 模型。
- 运行期凭据复用 `BOOKWIKI_CHAT_API_KEY` / `BOOKWIKI_CHAT_BASE_URL`(默认 `https://openrouter.ai/api/v1`);构建期用 `OPENROUTER_API_KEY`。缺失即报错,不做 mock/fallback。
- embedding 调用失败时,语义路降级为仅返回 trigram 关键词结果 + 告警(关键词为真实词法结果,非伪造)。
- ESM、`import { x } from 'x'`;避免 class;TS 过 oxlint。

---

## 文件结构

- `bookwiki/indexer/sqlite_builder.py`(改):trigram、`embedding` 列、`search_meta`、写入向量。
- `bookwiki/indexer/embedder.py`(新):OpenRouter passage 向量。
- `tests/test_search_hybrid.py`(新):trigram + schema + embedding 写入。
- `site-template/lib/vector.ts`(新):BLOB↔Float32、点积、topN。
- `site-template/lib/fusion.ts`(新):RRF。
- `site-template/lib/embedding.ts`(新):服务端 query 向量。
- `site-template/lib/rag.ts`(改):`toFtsQuery`、`searchChunks` async hybrid、降级。
- `site-template/lib/sqlite.ts`(改):缓存只读连接。
- `site-template/app/api/search/route.ts`(改):自定义中文后端 → SortedResult。
- `site-template/app/api/chat/route.ts`(改):`await searchChunks`。
- `site-template/{vitest.config.ts,package.json}`(改):vitest。
- `site-template/lib/*.test.ts`(新):TS 单测。

---

### Task 1: trigram 分词 + schema(embedding 列 + search_meta)

**Files:**
- Modify: `bookwiki/indexer/sqlite_builder.py`(`_create_schema`)
- Test: `tests/test_search_hybrid.py`

**Interfaces:**
- Produces:`chunks.embedding BLOB`(本任务暂为 NULL)、表 `search_meta(key TEXT PRIMARY KEY, value TEXT)`、FTS5 `tokenize='trigram'`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_search_hybrid.py
from __future__ import annotations

import sqlite3
from pathlib import Path

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
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_search_hybrid.py -v`
Expected: FAIL（`反向传播` 命中 0,或 `embedding`/`search_meta` 不存在）

- [ ] **Step 3: 改 schema**

在 `bookwiki/indexer/sqlite_builder.py` 的 `_create_schema` 里:
1. `chunks` 表 `token_count INTEGER` 后增加一行 `, embedding BLOB`。
2. FTS5 的 `tokenize='unicode61 remove_diacritics 2'` 改为 `tokenize='trigram'`。
3. 在 `_create_schema` 的 executescript 末尾(`CREATE VIEW documents` 之后)追加:

```sql
        CREATE TABLE search_meta
        (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
```

- [ ] **Step 4: 运行,确认通过**

Run: `pytest tests/test_search_hybrid.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add bookwiki/indexer/sqlite_builder.py tests/test_search_hybrid.py
git commit -m "feat(index): FTS5 改 trigram + 新增 embedding 列与 search_meta 表"
```

---

### Task 2: OpenRouter passage 向量 — `embedder.py`

**Files:**
- Create: `bookwiki/indexer/embedder.py`
- Test: `tests/test_search_hybrid.py`(追加)

**Interfaces:**
- Produces:
  - `embed_texts(texts: list[str], *, model: str, api_key: str, base_url: str) -> list[list[float]]`（调用 OpenRouter,返回每条 L2 normalized 向量）
  - `floats_to_blob(vec: list[float]) -> bytes`（float32 little-endian）
  - `DEFAULT_EMBED_MODEL = "baai/bge-m3"`,`DEFAULT_EMBED_DIM = 1024`

- [ ] **Step 1: 写失败测试(打桩 HTTP)**

```python
# 追加到 tests/test_search_hybrid.py
import math

from bookwiki.indexer import embedder


def test_floats_to_blob_roundtrip() -> None:
    import struct

    blob = embedder.floats_to_blob([1.0, 2.0, 3.0])
    assert len(blob) == 12
    assert list(struct.unpack("<3f", blob)) == [1.0, 2.0, 3.0]


def test_embed_texts_normalizes(monkeypatch) -> None:
    def fake_post(texts, *, model, api_key, base_url):
        return [[3.0, 4.0] for _ in texts]  # 未归一化,模长 5

    monkeypatch.setattr(embedder, "_raw_embed", fake_post)
    out = embedder.embed_texts(["a", "b"], model="m", api_key="k", base_url="u")
    assert len(out) == 2
    assert math.isclose(math.hypot(*out[0]), 1.0, rel_tol=1e-6)
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_search_hybrid.py -k "blob or normalize" -v`
Expected: FAIL（`No module named bookwiki.indexer.embedder`）

- [ ] **Step 3: 实现 embedder.py**

```python
# bookwiki/indexer/embedder.py
from __future__ import annotations

import math
import struct
import urllib.request
import json

DEFAULT_EMBED_MODEL = "baai/bge-m3"
DEFAULT_EMBED_DIM = 1024


def floats_to_blob(vec: list[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def _raw_embed(texts: list[str], *, model: str, api_key: str, base_url: str) -> list[list[float]]:
    payload = json.dumps({"model": model, "input": texts}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/embeddings",
        data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return [item["embedding"] for item in body["data"]]


def embed_texts(texts: list[str], *, model: str, api_key: str, base_url: str) -> list[list[float]]:
    if not texts:
        return []
    raw = _raw_embed(texts, model=model, api_key=api_key, base_url=base_url)
    return [_normalize(vec) for vec in raw]
```

- [ ] **Step 4: 运行,确认通过**

Run: `pytest tests/test_search_hybrid.py -k "blob or normalize" -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add bookwiki/indexer/embedder.py tests/test_search_hybrid.py
git commit -m "feat(index): 新增 OpenRouter passage 向量 embedder(归一化+blob)"
```

---

### Task 3: 把向量写进索引 + 写 search_meta

**Files:**
- Modify: `bookwiki/indexer/sqlite_builder.py`（`build_sqlite_index`、`_insert_chunks`、新增 `_insert_embeddings`、`_insert_search_meta`)
- Test: `tests/test_search_hybrid.py`(追加)

**Interfaces:**
- Consumes:`embedder.embed_texts`、`embedder.floats_to_blob`、`embedder.DEFAULT_EMBED_*`
- Produces:`build_sqlite_index(content_dir, db_path, *, embed=True, model=None, api_key=None, base_url=None)`；`embed=True` 时为每个 chunk 写 `embedding`,并写 `search_meta` 的 `embedding_model`/`embedding_dim`。

- [ ] **Step 1: 写失败测试(打桩 embedder)**

```python
# 追加到 tests/test_search_hybrid.py
def test_build_writes_embeddings_and_meta(tmp_path: Path, monkeypatch) -> None:
    from bookwiki.indexer import sqlite_builder

    def fake_embed(texts, *, model, api_key, base_url):
        return [[1.0] + [0.0] * 1023 for _ in texts]

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
```

- [ ] **Step 2: 运行,确认失败**

Run: `pytest tests/test_search_hybrid.py -k embeddings_and_meta -v`
Expected: FAIL（`build_sqlite_index() got an unexpected keyword 'embed'`)

- [ ] **Step 3: 实现**

在 `sqlite_builder.py` 顶部 import 增加 `from bookwiki.indexer import embedder`。

把 `build_sqlite_index` 签名与函数体改为:

```python
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
            _insert_embeddings(conn, resolved_model, api_key, base_url or "https://openrouter.ai/api/v1")
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
```

在文件末尾(`_optional_str` 之前)新增:

```python
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
```

- [ ] **Step 4: 运行,确认通过**

Run: `pytest tests/test_search_hybrid.py -v`
Expected: PASS（全部）

- [ ] **Step 5: 让 `index_node` 传入 embedding 开关**

修改 `bookwiki/pipeline/index.py` 的 `index_node`,从环境变量读取 key 并启用 embedding:

```python
import os

def index_node(state: State, cfg: BookConfig) -> State:
    db_path = cfg.site_dir / ".bookwiki" / "bookwiki.sqlite"
    _LOG.info("index: building sqlite db=%s", _rel(db_path, cfg.book_dir))
    api_key = os.environ.get("OPENROUTER_API_KEY")
    build_sqlite_index(
        cfg.content_dir,
        db_path,
        embed=bool(api_key),
        model=os.environ.get("BOOKWIKI_EMBED_MODEL"),
        api_key=api_key,
        base_url=os.environ.get("BOOKWIKI_EMBED_BASE_URL"),
    )
    size = db_path.stat().st_size if db_path.exists() else 0
    _LOG.info("index: done db_size_bytes=%d embed=%s", size, bool(api_key))
    return {"sqlite": _rel(db_path, cfg.book_dir)}
```

- [ ] **Step 6: 提交**

```bash
git add bookwiki/indexer/sqlite_builder.py bookwiki/pipeline/index.py tests/test_search_hybrid.py
git commit -m "feat(index): 构建期为 chunk 写入向量并记录 search_meta"
```

---

### Task 4: 引入 vitest

**Files:**
- Modify: `site-template/package.json`
- Create: `site-template/vitest.config.ts`
- Create: `site-template/lib/smoke.test.ts`（临时占位,验证 runner)

**Interfaces:**
- Produces:`pnpm --dir site-template test` 可运行 vitest。

- [ ] **Step 1: 安装 vitest**

Run: `pnpm --dir site-template add -D vitest`
Expected: 安装成功(devDependencies 出现 vitest)

- [ ] **Step 2: 加配置与脚本**

`site-template/vitest.config.ts`:

```ts
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    environment: 'node',
    include: ['lib/**/*.test.ts', 'app/**/*.test.ts'],
  },
});
```

在 `site-template/package.json` 的 `scripts` 增加:`"test": "vitest run"`。

`site-template/lib/smoke.test.ts`:

```ts
import { expect, test } from 'vitest';

test('vitest runs', () => {
  expect(1 + 1).toBe(2);
});
```

- [ ] **Step 3: 运行,确认通过**

Run: `pnpm --dir site-template test`
Expected: PASS（1 passed)

- [ ] **Step 4: 删占位并提交**

```bash
rm site-template/lib/smoke.test.ts
git add site-template/package.json site-template/vitest.config.ts site-template/pnpm-lock.yaml
git commit -m "chore(site): 引入 vitest 测试运行器"
```

---

### Task 5: 向量工具 — `vector.ts`

**Files:**
- Create: `site-template/lib/vector.ts`
- Test: `site-template/lib/vector.test.ts`

**Interfaces:**
- Produces:
  - `blobToFloat32(buf: Buffer | Uint8Array): Float32Array`
  - `dot(a: Float32Array, b: Float32Array): number`
  - `topNByDot(query: Float32Array, items: { id: string; vec: Float32Array }[], n: number): { id: string; score: number }[]`

- [ ] **Step 1: 写失败测试**

```ts
// site-template/lib/vector.test.ts
import { expect, test } from 'vitest';
import { blobToFloat32, dot, topNByDot } from './vector';

test('blobToFloat32 reads little-endian float32', () => {
  const src = new Float32Array([1, 2, 3]);
  const buf = Buffer.from(src.buffer);
  expect(Array.from(blobToFloat32(buf))).toEqual([1, 2, 3]);
});

test('dot computes inner product', () => {
  expect(dot(new Float32Array([1, 0, 1]), new Float32Array([1, 2, 3]))).toBe(4);
});

test('topNByDot ranks by score desc', () => {
  const q = new Float32Array([1, 0]);
  const items = [
    { id: 'a', vec: new Float32Array([0, 1]) },
    { id: 'b', vec: new Float32Array([1, 0]) },
  ];
  const out = topNByDot(q, items, 1);
  expect(out[0].id).toBe('b');
});
```

- [ ] **Step 2: 运行,确认失败**

Run: `pnpm --dir site-template test`
Expected: FAIL（找不到 `./vector`）

- [ ] **Step 3: 实现**

```ts
// site-template/lib/vector.ts
export function blobToFloat32(buf: Buffer | Uint8Array): Float32Array {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  const aligned = bytes.byteOffset % 4 === 0
    ? new Float32Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.byteLength / 4))
    : new Float32Array(bytes.slice().buffer);
  return aligned;
}

export function dot(a: Float32Array, b: Float32Array): number {
  let sum = 0;
  const len = Math.min(a.length, b.length);
  for (let i = 0; i < len; i += 1) sum += a[i] * b[i];
  return sum;
}

export function topNByDot(
  query: Float32Array,
  items: { id: string; vec: Float32Array }[],
  n: number,
): { id: string; score: number }[] {
  return items
    .map((item) => ({ id: item.id, score: dot(query, item.vec) }))
    .sort((a, b) => b.score - a.score)
    .slice(0, n);
}
```

- [ ] **Step 4: 运行,确认通过**

Run: `pnpm --dir site-template test`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add site-template/lib/vector.ts site-template/lib/vector.test.ts
git commit -m "feat(site): 新增向量工具(blob 解码/点积/topN)"
```

---

### Task 6: RRF 融合 — `fusion.ts`

**Files:**
- Create: `site-template/lib/fusion.ts`
- Test: `site-template/lib/fusion.test.ts`

**Interfaces:**
- Produces:`rrf(rankings: string[][], k?: number): { id: string; score: number }[]`（每个内层数组是按相关度从高到低的 id 列表)

- [ ] **Step 1: 写失败测试**

```ts
// site-template/lib/fusion.test.ts
import { expect, test } from 'vitest';
import { rrf } from './fusion';

test('rrf rewards items ranked high in multiple lists', () => {
  const kw = ['a', 'b', 'c'];
  const vec = ['b', 'a', 'd'];
  const out = rrf([kw, vec]);
  expect(out[0].id).toBe('a');
  expect(out.map((r) => r.id)).toContain('d');
});

test('rrf handles single-list items', () => {
  const out = rrf([['x'], ['y']]);
  const ids = out.map((r) => r.id);
  expect(ids).toEqual(expect.arrayContaining(['x', 'y']));
});
```

注:`a` 在 kw 第 1、vec 第 2;`b` 在 kw 第 2、vec 第 1 → 二者 RRF 同分。为让断言稳定,改用更明确的输入:

```ts
test('rrf ranks the consistently-high item first', () => {
  const kw = ['a', 'b', 'c', 'd'];
  const vec = ['a', 'c', 'b', 'd'];
  const out = rrf([kw, vec]);
  expect(out[0].id).toBe('a'); // 两表都第 1
});
```

(保留这条与上面的 single-list 一条;删除会并列的那条。)

- [ ] **Step 2: 运行,确认失败**

Run: `pnpm --dir site-template test`
Expected: FAIL（找不到 `./fusion`)

- [ ] **Step 3: 实现**

```ts
// site-template/lib/fusion.ts
export function rrf(rankings: string[][], k = 60): { id: string; score: number }[] {
  const scores = new Map<string, number>();
  for (const ranking of rankings) {
    ranking.forEach((id, index) => {
      scores.set(id, (scores.get(id) ?? 0) + 1 / (k + index + 1));
    });
  }
  return [...scores.entries()]
    .map(([id, score]) => ({ id, score }))
    .sort((a, b) => b.score - a.score);
}
```

- [ ] **Step 4: 运行,确认通过**

Run: `pnpm --dir site-template test`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add site-template/lib/fusion.ts site-template/lib/fusion.test.ts
git commit -m "feat(site): 新增 RRF 融合"
```

---

### Task 7: 服务端 query 向量 — `embedding.ts`

**Files:**
- Create: `site-template/lib/embedding.ts`
- Test: `site-template/lib/embedding.test.ts`

**Interfaces:**
- Produces:`embedQuery(query: string): Promise<Float32Array>`（POST OpenRouter `/embeddings`,L2 normalized;凭据来自 `BOOKWIKI_CHAT_API_KEY`/`BOOKWIKI_CHAT_BASE_URL`、模型来自 `BOOKWIKI_EMBED_MODEL`,默认 `baai/bge-m3`)。失败抛错。

- [ ] **Step 1: 写失败测试(mock fetch)**

```ts
// site-template/lib/embedding.test.ts
import { afterEach, expect, test, vi } from 'vitest';
import { embedQuery } from './embedding';

afterEach(() => vi.unstubAllGlobals());

test('embedQuery posts and returns normalized vector', async () => {
  process.env.BOOKWIKI_CHAT_API_KEY = 'k';
  const fetchMock = vi.fn(async () => ({
    ok: true,
    json: async () => ({ data: [{ embedding: [3, 4] }] }),
  }));
  vi.stubGlobal('fetch', fetchMock);
  const vec = await embedQuery('反向传播');
  expect(Math.hypot(vec[0], vec[1])).toBeCloseTo(1, 5);
  expect(fetchMock).toHaveBeenCalledOnce();
});

test('embedQuery throws on non-ok', async () => {
  process.env.BOOKWIKI_CHAT_API_KEY = 'k';
  vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 500, text: async () => 'boom' })));
  await expect(embedQuery('x')).rejects.toThrow();
});
```

- [ ] **Step 2: 运行,确认失败**

Run: `pnpm --dir site-template test`
Expected: FAIL（找不到 `./embedding`)

- [ ] **Step 3: 实现**

```ts
// site-template/lib/embedding.ts
const DEFAULT_MODEL = 'baai/bge-m3';
const DEFAULT_BASE_URL = 'https://openrouter.ai/api/v1';

function normalize(vec: number[]): Float32Array {
  let norm = 0;
  for (const v of vec) norm += v * v;
  norm = Math.sqrt(norm) || 1;
  return Float32Array.from(vec, (v) => v / norm);
}

export async function embedQuery(query: string): Promise<Float32Array> {
  const apiKey = process.env.BOOKWIKI_CHAT_API_KEY;
  if (!apiKey) throw new Error('BOOKWIKI_CHAT_API_KEY 未配置,无法计算 query 向量');
  const baseUrl = process.env.BOOKWIKI_CHAT_BASE_URL ?? DEFAULT_BASE_URL;
  const model = process.env.BOOKWIKI_EMBED_MODEL ?? DEFAULT_MODEL;

  const resp = await fetch(`${baseUrl.replace(/\/$/, '')}/embeddings`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ model, input: query }),
  });
  if (!resp.ok) {
    const detail = await resp.text().catch(() => '');
    throw new Error(`embedding 请求失败 ${resp.status}: ${detail}`);
  }
  const body = (await resp.json()) as { data: { embedding: number[] }[] };
  const embedding = body.data?.[0]?.embedding;
  if (!embedding) throw new Error('embedding 响应缺少向量');
  return normalize(embedding);
}
```

- [ ] **Step 4: 运行,确认通过**

Run: `pnpm --dir site-template test`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add site-template/lib/embedding.ts site-template/lib/embedding.test.ts
git commit -m "feat(site): 新增服务端 query 向量(OpenRouter)"
```

---

### Task 8: `rag.ts` 升级为 async hybrid + trigram 查询重写 + 降级

**Files:**
- Modify: `site-template/lib/rag.ts`
- Test: `site-template/lib/rag.test.ts`

**Interfaces:**
- Consumes:`embedQuery`(embedding.ts)、`blobToFloat32`/`topNByDot`(vector.ts)、`rrf`(fusion.ts)、`queryAll`(sqlite.ts)
- Produces:`searchChunks(query: string, limit?: number, chapterId?: string): Promise<SearchChunk[]>`(**改为 async**);`toFtsQuery` 适配 trigram。

- [ ] **Step 1: 写失败测试(真 better-sqlite3 临时库 + 打桩 embedQuery)**

```ts
// site-template/lib/rag.test.ts
import Database from 'better-sqlite3';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';

const dbHolder: { current: import('better-sqlite3').Database | null } = { current: null };

vi.mock('./sqlite', () => ({
  queryAll: <T>(sql: string, params: unknown[] = []) =>
    dbHolder.current!.prepare(sql).all(...(params as never[])) as T[],
}));
vi.mock('./embedding', () => ({
  embedQuery: vi.fn(),
}));

import { embedQuery } from './embedding';
import { searchChunks, toFtsQuery } from './rag';
import { floatsBlob } from './test-helpers';

function seed(): void {
  const db = new Database(':memory:');
  db.exec(`
    CREATE TABLE pages(id TEXT PRIMARY KEY, slug TEXT, title TEXT);
    CREATE TABLE chunks("rowid" INTEGER PRIMARY KEY AUTOINCREMENT, chunk_id TEXT, page_id TEXT,
      chapter_id TEXT, chunk_index INTEGER, heading_path TEXT, text TEXT, source_refs_json TEXT, embedding BLOB);
    CREATE VIRTUAL TABLE fts_chunks USING fts5(text, heading_path, content='chunks',
      content_rowid='rowid', tokenize='trigram');
  `);
  db.prepare('INSERT INTO pages VALUES (?,?,?)').run('p1', 'ch01', '反向传播');
  const insert = db.prepare(
    'INSERT INTO chunks(chunk_id,page_id,chapter_id,chunk_index,heading_path,text,source_refs_json,embedding) VALUES (?,?,?,?,?,?,?,?)',
  );
  insert.run('c1', 'p1', 'ch01', 0, '反向传播', '反向传播用于训练神经网络', '[]', floatsBlob([1, 0]));
  insert.run('c2', 'p1', 'ch01', 1, '梯度', '梯度下降优化损失函数', '[]', floatsBlob([0, 1]));
  db.exec("INSERT INTO fts_chunks(fts_chunks) VALUES('rebuild')");
  dbHolder.current = db;
}

beforeEach(seed);
afterEach(() => {
  dbHolder.current?.close();
  vi.clearAllMocks();
});

test('toFtsQuery: >=3 字用短语 OR;含 <3 字时跳过短词', () => {
  expect(toFtsQuery('反向传播 神经网络')).toBe('"反向传播" OR "神经网络"');
});

test('keyword path finds Chinese substring', async () => {
  (embedQuery as ReturnType<typeof vi.fn>).mockResolvedValue(new Float32Array([0, 0]));
  const out = await searchChunks('反向传播', 5);
  expect(out.some((c) => c.chunkId === 'c1')).toBe(true);
});

test('semantic path recalls by meaning even without keyword overlap', async () => {
  (embedQuery as ReturnType<typeof vi.fn>).mockResolvedValue(new Float32Array([0, 1]));
  const out = await searchChunks('损失函数怎么优化', 5);
  expect(out[0].chunkId).toBe('c2'); // 向量最近
});

test('degrades to keyword when embedding fails', async () => {
  (embedQuery as ReturnType<typeof vi.fn>).mockRejectedValue(new Error('down'));
  const out = await searchChunks('反向传播', 5);
  expect(out.some((c) => c.chunkId === 'c1')).toBe(true); // 不抛错,仍有词法结果
});
```

辅助:`site-template/lib/test-helpers.ts`:

```ts
export function floatsBlob(vec: number[]): Buffer {
  return Buffer.from(Float32Array.from(vec).buffer);
}
```

- [ ] **Step 2: 运行,确认失败**

Run: `pnpm --dir site-template test`
Expected: FAIL（`searchChunks` 非 async / 无语义 / `toFtsQuery` 行为不符)

- [ ] **Step 3: 重写 rag.ts 的相关部分**

在 `rag.ts` 顶部增加 import:

```ts
import { embedQuery } from './embedding';
import { rrf } from './fusion';
import { blobToFloat32, topNByDot } from './vector';
```

`toFtsQuery` 改为(短词 <3 退出 FTS,交给 LIKE):

```ts
function toFtsQuery(query: string) {
  const terms = query.split(/\s+/).filter((t) => t.length >= 3);
  if (terms.length === 0) return '';
  return terms.map((term) => `"${term.replaceAll('"', '""')}"`).join(' OR ');
}
```

`searchChunks` 改为 async hybrid:

```ts
export async function searchChunks(query: string, limit = 8, chapterId?: string) {
  const normalized = query.trim();
  if (!normalized) return [];

  // 1) 关键词路(trigram;空 FTS 时走 LIKE)
  const ftsQuery = toFtsQuery(normalized);
  const keywordRows = ftsQuery
    ? queryRows(ftsQuery, normalized, limit * 4, chapterId)
    : likeRows(normalized, limit * 4, chapterId);

  // 2) 语义路(失败则降级为仅关键词)
  let semanticIds: string[] = [];
  try {
    const queryVec = await embedQuery(normalized);
    const vectorRows = loadVectors(chapterId);
    semanticIds = topNByDot(
      queryVec,
      vectorRows.map((r) => ({ id: r.chunk_id, vec: blobToFloat32(r.embedding) })),
      limit * 4,
    ).map((r) => r.id);
  } catch (error) {
    console.warn('[search] 语义路降级为关键词:', error instanceof Error ? error.message : error);
  }

  // 3) RRF 融合;无语义时等价于关键词排序
  const keywordIds = keywordRows.map((r) => r.chunk_id);
  const fused = rrf(semanticIds.length ? [keywordIds, semanticIds] : [keywordIds]);

  const byId = new Map(keywordRows.map((r) => [r.chunk_id, r]));
  for (const r of loadRowsByIds(fused.map((f) => f.id).filter((id) => !byId.has(id)))) {
    byId.set(r.chunk_id, r);
  }

  return fused
    .map((f) => byId.get(f.id))
    .filter((r): r is ChunkRow => Boolean(r))
    .slice(0, limit)
    .map(rowToChunk) satisfies SearchChunk[];
}
```

新增辅助(放在 `queryRows` 附近):

```ts
function likeRows(rawQuery: string, limit: number, chapterId?: string) {
  const like = `%${rawQuery}%`;
  const chapterClause = chapterId ? 'AND chunks.chapter_id = ?' : '';
  const params = chapterId ? [like, chapterId, limit] : [like, limit];
  return queryAll<ChunkRow>(
    `SELECT chunks.chunk_id, chunks.page_id, chunks.chapter_id, pages.title, pages.slug,
            chunks.heading_path, chunks.text, chunks.source_refs_json
     FROM chunks JOIN pages ON pages.id = chunks.page_id
     WHERE chunks.text LIKE ? ${chapterClause} LIMIT ?`,
    params,
  );
}

function loadVectors(chapterId?: string) {
  const chapterClause = chapterId ? 'WHERE chapter_id = ?' : '';
  const params = chapterId ? [chapterId] : [];
  return queryAll<{ chunk_id: string; embedding: Buffer }>(
    `SELECT chunk_id, embedding FROM chunks WHERE embedding IS NOT NULL ${chapterId ? 'AND chapter_id = ?' : ''}`,
    params,
  );
}

function loadRowsByIds(ids: string[]) {
  if (ids.length === 0) return [];
  const placeholders = ids.map(() => '?').join(',');
  return queryAll<ChunkRow>(
    `SELECT chunks.chunk_id, chunks.page_id, chunks.chapter_id, pages.title, pages.slug,
            chunks.heading_path, chunks.text, chunks.source_refs_json
     FROM chunks JOIN pages ON pages.id = chunks.page_id
     WHERE chunks.chunk_id IN (${placeholders})`,
    ids,
  );
}

function rowToChunk(row: ChunkRow) {
  return {
    chunkId: row.chunk_id,
    pageId: row.page_id,
    chapterId: row.chapter_id,
    title: row.title,
    slug: row.slug,
    headingPath: row.heading_path,
    text: row.text,
    sourceRefs: parseSourceRefs(row.source_refs_json),
  };
}
```

把 `queryRows` 改为只负责 FTS(去掉它内部的 catch→LIKE,因为 LIKE 现在是 `likeRows`;FTS 抛错时回退 `likeRows`):

```ts
function queryRows(ftsQuery: string, rawQuery: string, limit: number, chapterId?: string) {
  const chapterClause = chapterId ? 'AND chunks.chapter_id = ?' : '';
  const params = chapterId ? [ftsQuery, chapterId, limit] : [ftsQuery, limit];
  try {
    return queryAll<ChunkRow>(
      `SELECT chunks.chunk_id, chunks.page_id, chunks.chapter_id, pages.title, pages.slug,
              chunks.heading_path, chunks.text, chunks.source_refs_json
       FROM chunks JOIN pages ON pages.id = chunks.page_id
       JOIN fts_chunks ON chunks.rowid = fts_chunks.rowid
       WHERE fts_chunks MATCH ? ${chapterClause} ORDER BY rank LIMIT ?`,
      params,
    );
  } catch {
    return likeRows(rawQuery, limit, chapterId);
  }
}
```

并把 `searchChunks` 旧的同步实现整体替换。`SearchChunk`/`ChunkRow` 类型沿用现有定义。

- [ ] **Step 4: 运行,确认通过**

Run: `pnpm --dir site-template test`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add site-template/lib/rag.ts site-template/lib/rag.test.ts site-template/lib/test-helpers.ts
git commit -m "feat(site): searchChunks 升级为 trigram+语义 hybrid(RRF,失败降级)"
```

---

### Task 9: 重写 `/api/search` + 缓存 SQLite 连接

**Files:**
- Modify: `site-template/app/api/search/route.ts`
- Modify: `site-template/lib/sqlite.ts`
- Test: `site-template/app/api/search/route.test.ts`

**Interfaces:**
- Consumes:`searchChunks`(rag.ts)
- Produces:`GET(request: Request): Promise<Response>`,返回 `SortedResult[]`。

- [ ] **Step 1: 写失败测试(打桩 rag)**

```ts
// site-template/app/api/search/route.test.ts
import { afterEach, expect, test, vi } from 'vitest';

vi.mock('@/lib/rag', () => ({
  searchChunks: vi.fn(async () => [
    {
      chunkId: 'c1', pageId: 'p1', chapterId: 'ch01', title: '反向传播',
      slug: 'ch01', headingPath: '反向传播 > 概述', text: '反向传播用于训练神经网络', sourceRefs: [],
    },
  ]),
}));

import { GET } from './route';

afterEach(() => vi.clearAllMocks());

test('GET returns SortedResult array with page and text entries', async () => {
  const res = await GET(new Request('http://x/api/search?query=反向传播'));
  const data = (await res.json()) as { id: string; type: string; url: string; content: string }[];
  expect(Array.isArray(data)).toBe(true);
  expect(data.some((d) => d.type === 'page')).toBe(true);
  expect(data.some((d) => d.type === 'text')).toBe(true);
  expect(data.every((d) => typeof d.url === 'string')).toBe(true);
});

test('GET empty query returns empty array', async () => {
  const res = await GET(new Request('http://x/api/search?query='));
  expect(await res.json()).toEqual([]);
});
```

- [ ] **Step 2: 运行,确认失败**

Run: `pnpm --dir site-template test`
Expected: FAIL（旧 route 无具名 `GET(Request)`)

- [ ] **Step 3: 重写 route.ts**

```ts
// site-template/app/api/search/route.ts
import type { SortedResult } from 'fumadocs-core/search';
import { searchChunks } from '@/lib/rag';

export async function GET(request: Request): Promise<Response> {
  const query = new URL(request.url).searchParams.get('query')?.trim() ?? '';
  if (!query) return Response.json([] satisfies SortedResult[]);

  const chunks = await searchChunks(query, 8);
  const results: SortedResult[] = [];
  const seenPages = new Set<string>();

  for (const chunk of chunks) {
    const pageUrl = `/docs/${chunk.slug}`;
    if (!seenPages.has(chunk.slug)) {
      seenPages.add(chunk.slug);
      results.push({ id: `page:${chunk.slug}`, type: 'page', content: chunk.title, url: pageUrl });
    }
    results.push({
      id: chunk.chunkId,
      type: 'text',
      content: chunk.text,
      url: pageUrl,
    });
  }
  return Response.json(results);
}
```

- [ ] **Step 4: 运行,确认通过**

Run: `pnpm --dir site-template test`
Expected: PASS

- [ ] **Step 5: 缓存只读连接(sqlite.ts)**

把 `site-template/lib/sqlite.ts` 的 `queryAll` 改为复用单例连接:

```ts
import Database from 'better-sqlite3';
import fs from 'node:fs';
import path from 'node:path';

type SqliteValue = string | number | null;

export function sqlitePath() {
  return path.join(/* turbopackIgnore: true */ process.cwd(), '.bookwiki', 'bookwiki.sqlite');
}

let cached: Database.Database | null = null;

function db(): Database.Database {
  if (cached) return cached;
  const file = sqlitePath();
  if (!fs.existsSync(file)) {
    throw new Error(`BookWiki SQLite database not found at ${file}`);
  }
  cached = new Database(file, { readonly: true, fileMustExist: true });
  return cached;
}

export function openReadonlyDb() {
  return db();
}

export function queryAll<T>(sql: string, params: SqliteValue[] = []) {
  return db().prepare(sql).all(...params) as T[];
}
```

- [ ] **Step 6: 运行全部 TS 测试**

Run: `pnpm --dir site-template test`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add site-template/app/api/search/route.ts site-template/app/api/search/route.test.ts site-template/lib/sqlite.ts
git commit -m "feat(site): /api/search 改为中文 hybrid 后端 + 缓存只读连接"
```

---

### Task 10: 聊天 `await searchChunks`

**Files:**
- Modify: `site-template/app/api/chat/route.ts`

**Interfaces:**
- Consumes:`searchChunks`(现为 async)

- [ ] **Step 1: 改调用**

在 `site-template/app/api/chat/route.ts` 的 `search_book` 的 `execute` 内,把:

```ts
const chunks = searchChunks(query, limit, requestedChapterId ?? chapterId);
```

改为:

```ts
const chunks = await searchChunks(query, limit, requestedChapterId ?? chapterId);
```

- [ ] **Step 2: 类型检查**

Run: `pnpm --dir site-template exec tsc --noEmit`
Expected: 无与 `searchChunks` 相关的类型错误(`await` 已匹配 Promise)

- [ ] **Step 3: 提交**

```bash
git add site-template/app/api/chat/route.ts
git commit -m "fix(chat): await 异步 searchChunks"
```

---

### Task 11: 收尾 — lint 与全量校验

- [ ] **Step 1: Python 测试**

Run: `pytest tests/test_search_hybrid.py -v`
Expected: PASS

- [ ] **Step 2: TS 测试 + lint + 类型**

Run: `pnpm --dir site-template test && pnpm --dir site-template run lint && pnpm --dir site-template exec tsc --noEmit`
Expected: 全 PASS / 无 error

- [ ] **Step 3: 提交(如有 lint 修复)**

```bash
git add -A && git commit -m "chore: lint/format 收尾"
```
