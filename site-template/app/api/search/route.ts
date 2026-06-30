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
    results.push({ id: chunk.chunkId, type: 'text', content: chunk.text, url: pageUrl });
  }

  return Response.json(results);
}
