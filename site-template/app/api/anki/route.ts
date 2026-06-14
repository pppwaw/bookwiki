import { exportAnkiCsv } from '@/lib/anki';

export const runtime = 'nodejs';

/**
 * GET /api/anki
 *
 * Streams an Anki-importable CSV from the read-only `card_items` SQLite index
 * that already backs the on-page <AnkiDeck>. Scope, most specific first:
 * - `?pagePath=/docs/...` or `?slug=...` → cards on that one page
 * - `?chapterId=<id>`                    → every card in that chapter
 * - no params                            → the whole book
 */
export function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url);
    const chapterId = searchParams.get('chapterId')?.trim() || undefined;
    const pagePath = searchParams.get('pagePath')?.trim() || undefined;
    const slug = pagePath ? slugFromPagePath(pagePath) : searchParams.get('slug')?.trim() || undefined;

    const { csv, filename, cardCount } = exportAnkiCsv({ chapterId, slug });

    return new Response(csv, {
      status: 200,
      headers: {
        'Content-Type': 'text/csv; charset=utf-8',
        'Content-Disposition': `attachment; filename="${filename}"`,
        'X-Anki-Card-Count': String(cardCount),
        'Cache-Control': 'no-store',
      },
    });
  } catch (error) {
    return Response.json(
      { error: error instanceof Error ? error.message : 'anki export failed' },
      { status: 503 },
    );
  }
}

/** Turn a docs pathname (`/docs/chapters/ch-1`) into a card `page_id` (`chapters/ch-1`). */
function slugFromPagePath(pagePath: string): string | undefined {
  const clean = pagePath.split(/[?#]/, 1)[0]?.replace(/\/+$/, '') ?? '';
  if (!clean.startsWith('/docs/')) return undefined;
  const slug = clean.slice('/docs/'.length);
  return slug.length ? slug : undefined;
}
