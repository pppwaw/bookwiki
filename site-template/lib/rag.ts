import { queryAll } from './sqlite';

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

export function contextFromChunks(chunks: SearchChunk[]) {
  return chunks
    .map((chunk, index) => {
      const sources = chunk.sourceRefs.length ? ` sources=${chunk.sourceRefs.join(',')}` : '';
      return `<chunk index="${index + 1}" page="${chunk.slug}"${sources}>\n${chunk.text}\n</chunk>`;
    })
    .join('\n\n');
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
