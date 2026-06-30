/**
 * Convert a raw MDX/Markdown chunk into a clean plain-text snippet for search
 * display. The fumadocs search dialog renders `content` as plain text (not MDX),
 * so component tags, math delimiters and markdown markup must be stripped or they
 * show up verbatim. Matching still runs on the raw text — this only affects what
 * the dialog shows.
 */
export function toPlainSnippet(raw: string, maxLen = 180): string {
  let s = raw;

  // Math: block then inline → drop (TeX is unreadable as plain text).
  s = s.replace(/\$\$[\s\S]*?\$\$/g, ' ').replace(/\$[^$\n]*\$/g, ' ');

  // MDX/JSX tags incl. their attribute blobs (summary={...}, rubric={[...]}).
  // Self-closing first, then paired open/close tags — children text is kept.
  s = s.replace(/<[A-Za-z][\w.]*\b[^>]*\/>/g, ' ');
  s = s.replace(/<\/?[A-Za-z][\w.]*\b[^>]*>/g, ' ');

  // Leftover JSX expression braces.
  s = s.replace(/\{[^{}]*\}/g, ' ');

  // Markdown line markers: headings, blockquotes, list/ordered bullets.
  s = s
    .replace(/^[ \t]*#{1,6}[ \t]+/gm, '')
    .replace(/^[ \t]*>[ \t]?/gm, '')
    .replace(/^[ \t]*[-*+][ \t]+/gm, '')
    .replace(/^[ \t]*\d+\.[ \t]+/gm, '');

  // Inline markup: code, emphasis, links.
  s = s
    .replace(/`([^`]*)`/g, '$1')
    .replace(/\*\*([^*]*)\*\*/g, '$1')
    .replace(/\*([^*]*)\*/g, '$1')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1');

  // Collapse whitespace and truncate.
  s = s.replace(/\s+/g, ' ').trim();
  if (s.length > maxLen) s = `${s.slice(0, maxLen).trimEnd()}…`;
  return s;
}
