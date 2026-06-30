import { embedQuery } from './embedding';
import { rrf } from './fusion';
import { queryAll } from './sqlite';
import { getLLMText, getSourcePage } from './source';
import { blobToFloat32, topNByDot } from './vector';

export type SearchChunk = {
  chunkId: string;
  pageId: string;
  chapterId: string | null;
  title: string;
  slug: string;
  headingPath: string | null;
  text: string;
  sourceRefs: string[];
};

export type CurrentArticle = {
  slug: string;
  title: string;
  text: string;
  sourceRefs: string[];
};

type ChunkRow = {
  chunk_id: string;
  page_id: string;
  chapter_id: string | null;
  title: string;
  slug: string;
  heading_path: string | null;
  text: string;
  source_refs_json: string;
};

type PageSourceRow = {
  source_refs_json: string;
};

type PageTitleRow = {
  slug: string;
  title: string;
};

/**
 * Resolve a page slug (e.g. `concepts/Self-Inductance`) to its title for page
 * citations. Returns null when the slug is unknown so callers can avoid
 * surfacing dead references.
 */
export function pageBySlug(slug: string) {
  const normalized = slug.trim();
  if (!normalized) return null;

  const rows = queryAll<PageTitleRow>(
    'SELECT slug, title FROM pages WHERE slug = ? LIMIT 1',
    [normalized],
  );
  return rows[0] ?? null;
}

export async function searchChunks(query: string, limit = 8, chapterId?: string) {
  const normalized = query.trim();
  if (!normalized) return [];

  // 1) 关键词路(trigram;无可用 ≥3 字短语时走 LIKE)
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

  const byId = new Map<string, ChunkRow>(keywordRows.map((r) => [r.chunk_id, r]));
  const missing = fused.map((f) => f.id).filter((id) => !byId.has(id));
  for (const r of loadRowsByIds(missing)) byId.set(r.chunk_id, r);

  return fused
    .map((f) => byId.get(f.id))
    .filter((r): r is ChunkRow => Boolean(r))
    .slice(0, limit)
    .map(rowToChunk) satisfies SearchChunk[];
}

export async function currentArticleFromPath(pagePath?: string, maxChars?: number) {
  const slug = slugFromPagePath(pagePath);
  if (slug === undefined) return null;

  const page = getSourcePage(slug.length ? slug.split('/') : undefined);
  if (!page) return null;

  const fullText = await getLLMText(page);
  const text = maxChars && maxChars > 0 ? truncateText(fullText, maxChars) : fullText;

  return {
    slug: page.url.replace(/^\/docs\/?/, '') || 'index',
    title: page.data.title,
    text,
    sourceRefs: sourceRefsForSlug(page.url.replace(/^\/docs\/?/, '') || 'index'),
  } satisfies CurrentArticle;
}

const CHUNK_SELECT = `
  SELECT
    chunks.chunk_id,
    chunks.page_id,
    chunks.chapter_id,
    pages.title,
    pages.slug,
    chunks.heading_path,
    chunks.text,
    chunks.source_refs_json
  FROM chunks
  JOIN pages ON pages.id = chunks.page_id
`;

function queryRows(ftsQuery: string, rawQuery: string, limit: number, chapterId?: string) {
  const chapterClause = chapterId ? 'AND chunks.chapter_id = ?' : '';
  const params = chapterId ? [ftsQuery, chapterId, limit] : [ftsQuery, limit];

  try {
    return queryAll<ChunkRow>(
      `${CHUNK_SELECT}
       JOIN fts_chunks ON chunks.rowid = fts_chunks.rowid
       WHERE fts_chunks MATCH ? ${chapterClause}
       ORDER BY rank
       LIMIT ?`,
      params,
    );
  } catch {
    return likeRows(rawQuery, limit, chapterId);
  }
}

function likeRows(rawQuery: string, limit: number, chapterId?: string) {
  const like = `%${rawQuery}%`;
  const chapterClause = chapterId ? 'AND chunks.chapter_id = ?' : '';
  const params = chapterId ? [like, chapterId, limit] : [like, limit];
  return queryAll<ChunkRow>(
    `${CHUNK_SELECT} WHERE chunks.text LIKE ? ${chapterClause} LIMIT ?`,
    params,
  );
}

function loadVectors(chapterId?: string) {
  const where = chapterId
    ? 'WHERE embedding IS NOT NULL AND chapter_id = ?'
    : 'WHERE embedding IS NOT NULL';
  const params = chapterId ? [chapterId] : [];
  return queryAll<{ chunk_id: string; embedding: Buffer }>(
    `SELECT chunk_id, embedding FROM chunks ${where}`,
    params,
  );
}

function loadRowsByIds(ids: string[]) {
  if (ids.length === 0) return [];
  const placeholders = ids.map(() => '?').join(',');
  return queryAll<ChunkRow>(
    `${CHUNK_SELECT} WHERE chunks.chunk_id IN (${placeholders})`,
    ids,
  );
}

function rowToChunk(row: ChunkRow): SearchChunk {
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

export function toFtsQuery(query: string) {
  const terms = query.split(/\s+/).filter((term) => term.length >= 3);
  if (terms.length === 0) return '';
  return terms.map((term) => `"${term.replaceAll('"', '""')}"`).join(' OR ');
}

function parseSourceRefs(value: string) {
  try {
    const parsed = JSON.parse(value) as unknown;
    return Array.isArray(parsed)
      ? parsed.filter((item): item is string => typeof item === 'string')
      : [];
  } catch {
    return [];
  }
}

function slugFromPagePath(pagePath?: string) {
  if (!pagePath) return undefined;
  const clean = pagePath.split(/[?#]/, 1)[0]?.replace(/\/+$/, '') ?? '';
  if (!clean || clean === '/docs') return '';
  if (!clean.startsWith('/docs/')) return undefined;
  return clean.slice('/docs/'.length);
}

function sourceRefsForSlug(slug: string) {
  const rows = queryAll<PageSourceRow>(
    `
    SELECT chunks.source_refs_json
    FROM chunks
    JOIN pages ON pages.id = chunks.page_id
    WHERE pages.slug = ?
    ORDER BY chunks.chunk_index
    `,
    [slug],
  );
  const refs: string[] = [];
  for (const row of rows) {
    for (const ref of parseSourceRefs(row.source_refs_json)) {
      if (!refs.includes(ref)) refs.push(ref);
    }
  }
  return refs;
}

function truncateText(value: string, maxChars: number) {
  if (value.length <= maxChars) return value;
  return `${value.slice(0, maxChars)}\n\n[truncated after ${maxChars} characters]`;
}
