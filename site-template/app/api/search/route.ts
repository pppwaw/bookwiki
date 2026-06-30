import type { SortedResult } from 'fumadocs-core/search';
import { searchChunks } from '@/lib/rag';
import { toPlainSnippet } from '@/lib/snippet';

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
    // The dialog renders `content` as plain text, so strip MDX/markdown/math.
    const snippet = toPlainSnippet(chunk.text);
    if (snippet) {
      results.push({ id: chunk.chunkId, type: 'text', content: snippet, url: pageUrl });
    }
  }

  return Response.json(results);
}
