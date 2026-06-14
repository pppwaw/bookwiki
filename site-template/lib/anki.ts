import { queryAll } from './sqlite';

export type AnkiExport = {
  filename: string;
  csv: string;
  cardCount: number;
};

/**
 * Selects which cards to export:
 * - `slug`: a page id (e.g. `chapters/chapter-11/chapter-11-5`) — exports the
 *   cards on exactly that page (what the on-page <AnkiDeck> shows).
 * - `chapterId`: a chapter id — exports every card in that chapter.
 * - neither: the whole book.
 *
 * `slug` wins when both are present (it is the more specific filter).
 */
export type AnkiFilter = {
  chapterId?: string;
  slug?: string;
};

type CardRow = {
  id: string;
  chapter_id: string;
  page_id: string;
  front: string;
  back: string;
  tags_json: string;
  source_refs_json: string;
};

const ANKI_SEPARATOR = ',';

/**
 * Build an Anki-importable CSV for one page (`slug`), one chapter (`chapterId`),
 * or the whole book (empty filter). Reads the already-indexed `card_items`
 * table; the route layer turns the result into a file download.
 */
export function exportAnkiCsv(filter: AnkiFilter = {}): AnkiExport {
  const rows = fetchCards(filter);
  const label = exportLabel(filter, rows);
  const deck = label ? `BookWiki::${sanitizeTag(label)}` : 'BookWiki';
  const lines = ankiHeader(deck);

  for (const row of rows) {
    const front = mdxToAnkiHtml(row.front);
    const back = mdxToAnkiHtml(row.back);
    if (!front && !back) continue;
    lines.push([csvField(front), csvField(back), csvField(cardTags(row))].join(ANKI_SEPARATOR));
  }

  return {
    filename: `anki-${label ? sanitizeFilename(label) : 'all'}.csv`,
    // Anki expects a trailing newline after the final record.
    csv: `${lines.join('\n')}\n`,
    cardCount: Math.max(lines.length - ANKI_HEADER_LINES, 0),
  };
}

/**
 * Whether the book has any flashcards at all. Used by the home page to decide
 * if the whole-book export entry point should show. Build-safe: a missing or
 * unreadable index yields `false` instead of throwing (so it never breaks a
 * static prerender).
 */
export function hasAnyCards(): boolean {
  try {
    const rows = queryAll<{ n: number }>('SELECT COUNT(*) AS n FROM card_items');
    return (rows[0]?.n ?? 0) > 0;
  } catch {
    return false;
  }
}

/**
 * Human-facing name used for the Anki deck + download filename. A page export
 * borrows the chapter id of its cards (they all share one) so a per-page deck
 * still reads as a chapter; falls back to the slug's last segment when empty.
 */
function exportLabel(filter: AnkiFilter, rows: CardRow[]): string | undefined {
  if (filter.slug) {
    return rows[0]?.chapter_id ?? lastSegment(filter.slug);
  }
  return filter.chapterId;
}

function fetchCards(filter: AnkiFilter): CardRow[] {
  let where = '';
  const params: string[] = [];

  if (filter.slug) {
    where = 'WHERE page_id = ?';
    params.push(filter.slug);
  } else if (filter.chapterId) {
    where = 'WHERE chapter_id = ?';
    params.push(filter.chapterId);
  }

  return queryAll<CardRow>(
    `
    SELECT id, chapter_id, page_id, front, back, tags_json, source_refs_json
    FROM card_items
    ${where}
    ORDER BY chapter_id, page_id, id
    `,
    params,
  );
}

function lastSegment(slug: string): string {
  const parts = slug.split('/').filter(Boolean);
  return parts[parts.length - 1] ?? slug;
}

const ANKI_HEADER_LINES = 5;

function ankiHeader(deck: string): string[] {
  // Modern Anki (2.1.55+) reads these `#`-prefixed directives at the top of the
  // file, so the import is one click with no manual field mapping.
  return [
    '#separator:Comma',
    '#html:true',
    '#notetype:Basic',
    `#deck:${deck}`,
    '#tags column:3',
  ];
}

function cardTags(row: CardRow): string {
  const tags = ['bookwiki', sanitizeTag(row.chapter_id)];
  for (const ref of parseJsonStringArray(row.source_refs_json)) {
    tags.push(sanitizeTag(ref));
  }
  for (const tag of parseJsonStringArray(row.tags_json)) {
    tags.push(sanitizeTag(tag));
  }
  // Anki separates tags by spaces; dedupe and drop empties.
  return [...new Set(tags.filter(Boolean))].join(' ');
}

/**
 * Reduce a raw MDX card face to Anki-friendly HTML: normalise math delimiters
 * to MathJax, drop JSX component wrappers (keeping their text), convert the few
 * inline Markdown markers that matter, and turn newlines into `<br>`.
 */
export function mdxToAnkiHtml(input: string): string {
  let text = input.trim();
  if (!text) return '';

  // 1. Stash math spans so later transforms never touch LaTeX internals.
  const math: string[] = [];
  const stash = (latex: string): string => {
    // Private-use sentinel (not a control char) so later transforms skip math.
    const token = `\uE000M${math.length}\uE000`;
    math.push(latex);
    return token;
  };
  text = text.replace(/\$\$([\s\S]+?)\$\$/g, (_match, body: string) => stash(`\\[${body.trim()}\\]`));
  text = text.replace(/\$([^$\n]+?)\$/g, (_match, body: string) => stash(`\\(${body.trim()}\\)`));

  // 2. Strip paired JSX components (e.g. <PreviewLink>text</PreviewLink>), keep inner text.
  const paired = /<([A-Z][A-Za-z0-9]*)\b[^>]*>([\s\S]*?)<\/\1>/g;
  let previous: string;
  do {
    previous = text;
    text = text.replace(paired, '$2');
  } while (text !== previous);

  // 3. Drop self-closing JSX components (e.g. <SourceRef id="..." />).
  text = text.replace(/<[A-Z][A-Za-z0-9]*\b[^>]*\/>/g, '');

  // 4. Markdown links -> visible text (relative slugs would not resolve in Anki).
  text = text.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1');

  // 5. Inline emphasis / code -> HTML.
  text = text.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  text = text.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<i>$2</i>');
  text = text.replace(/`([^`]+)`/g, '<code>$1</code>');

  // 6. Collapse blank lines, then newlines -> <br>.
  text = text.replace(/\r\n/g, '\n').replace(/\n{2,}/g, '\n').trim();
  text = text.replace(/\n/g, '<br>');

  // 7. Restore math spans.
  text = text.replace(/\uE000M(\d+)\uE000/g, (_match, index: string) => math[Number(index)] ?? '');

  return text;
}

function csvField(value: string): string {
  if (/[",\r\n]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

function sanitizeTag(value: string): string {
  return value.trim().replace(/\s+/g, '_');
}

function sanitizeFilename(value: string): string {
  const cleaned = value.trim().replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^-+|-+$/g, '');
  return cleaned || 'chapter';
}

function parseJsonStringArray(value: string): string[] {
  try {
    const parsed = JSON.parse(value) as unknown;
    return Array.isArray(parsed) ? parsed.filter((item): item is string => typeof item === 'string') : [];
  } catch {
    return [];
  }
}
