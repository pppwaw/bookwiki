import { queryAll } from './sqlite';
import { getLLMText, getSourcePage } from './source';

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

export function searchChunks(query: string, limit = 8, chapterId?: string) {
  const normalized = query.trim();

  if (!normalized) {
    return [];
  }

  const ftsQuery = toFtsQuery(normalized);
  const rows = queryRows(ftsQuery, normalized, limit, chapterId);

  return rows.map((row) => ({
    chunkId: row.chunk_id,
    pageId: row.page_id,
    chapterId: row.chapter_id,
    title: row.title,
    slug: row.slug,
    headingPath: row.heading_path,
    text: row.text,
    sourceRefs: parseSourceRefs(row.source_refs_json),
  })) satisfies SearchChunk[];
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

function queryRows(ftsQuery: string, rawQuery: string, limit: number, chapterId?: string) {
  const chapterClause = chapterId ? 'AND chunks.chapter_id = ?' : '';
  const params = chapterId ? [ftsQuery, chapterId, limit] : [ftsQuery, limit];

  try {
    return queryAll<ChunkRow>(
      `
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
      JOIN fts_chunks ON chunks.rowid = fts_chunks.rowid
      WHERE fts_chunks MATCH ? ${chapterClause}
      ORDER BY rank
      LIMIT ?
      `,
      params,
    );
  } catch {
    const like = `%${rawQuery}%`;
    const fallbackParams = chapterId ? [like, chapterId, limit] : [like, limit];

    return queryAll<ChunkRow>(
      `
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
      WHERE chunks.text LIKE ? ${chapterClause}
      LIMIT ?
      `,
      fallbackParams,
    );
  }
}

function toFtsQuery(query: string) {
  return query
    .split(/\s+/)
    .filter(Boolean)
    .map((term) => `"${term.replaceAll('"', '""')}"`)
    .join(' OR ');
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
