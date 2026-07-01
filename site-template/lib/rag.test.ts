import Database from 'better-sqlite3';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { floatsBlob } from './test-helpers';

const { dbHolder } = vi.hoisted(() => ({
  dbHolder: { current: null as import('better-sqlite3').Database | null },
}));

vi.mock('./sqlite', () => ({
  queryAll: (sql: string, params: unknown[] = []) =>
    dbHolder.current!.prepare(sql).all(...(params as never[])),
}));
vi.mock('./source', () => ({ getLLMText: vi.fn(), getSourcePage: vi.fn() }));
vi.mock('./embedding', () => ({ embedQuery: vi.fn() }));

import { embedQuery } from './embedding';
import { pageBySlug, searchChunks, toFtsQuery } from './rag';

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
  // DB stores raw (unencoded) Chinese slugs; the pipeline indexer never encodes.
  db.prepare('INSERT INTO pages VALUES (?,?,?)').run('p2', '概念/感应', '电磁感应');
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

test('toFtsQuery: >=3 字用短语 OR 连接', () => {
  expect(toFtsQuery('反向传播 神经网络')).toBe('"反向传播" OR "神经网络"');
});

test('keyword path finds Chinese substring', async () => {
  vi.mocked(embedQuery).mockResolvedValue(new Float32Array([0, 0]));
  const out = await searchChunks('反向传播', 5);
  expect(out.some((c) => c.chunkId === 'c1')).toBe(true);
});

test('semantic path recalls by meaning even without keyword overlap', async () => {
  vi.mocked(embedQuery).mockResolvedValue(new Float32Array([0, 1]));
  const out = await searchChunks('损失函数怎么优化', 5);
  expect(out[0].chunkId).toBe('c2');
});

test('degrades to keyword when embedding fails', async () => {
  vi.mocked(embedQuery).mockRejectedValue(new Error('down'));
  const out = await searchChunks('反向传播', 5);
  expect(out.some((c) => c.chunkId === 'c1')).toBe(true);
});

test('pageBySlug resolves a raw Chinese slug', () => {
  expect(pageBySlug('概念/感应')?.title).toBe('电磁感应');
});

test('pageBySlug resolves a percent-encoded slug against raw DB rows', () => {
  // The model cites the current article slug, which fumadocs percent-encodes.
  const encoded = '概念/感应'.split('/').map(encodeURIComponent).join('/');
  expect(pageBySlug(encoded)?.title).toBe('电磁感应');
});
