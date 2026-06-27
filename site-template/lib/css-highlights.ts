/**
 * Thin typed wrapper over the CSS Custom Highlight API. Painting ranges via
 * `CSS.highlights` keeps the DOM untouched (no `<mark>` wrappers), so it never
 * fights React's ownership of the article DOM. Styling lives in `global.css`
 * under `::highlight(bookwiki-hl-<color>)`.
 *
 * The API is accessed through narrow casts rather than ambient globals to stay
 * build-safe regardless of the TS lib.dom version.
 */
type HighlightConstructor = new (...ranges: Range[]) => unknown;
type HighlightRegistry = {
  set(name: string, highlight: unknown): void;
  delete(name: string): void;
};

export function supportsHighlights(): boolean {
  return (
    typeof CSS !== 'undefined' &&
    'highlights' in CSS &&
    typeof (globalThis as { Highlight?: unknown }).Highlight === 'function'
  );
}

function registry(): HighlightRegistry | null {
  if (!supportsHighlights()) return null;
  return (CSS as unknown as { highlights: HighlightRegistry }).highlights;
}

export function setHighlight(name: string, ranges: Range[]): void {
  const reg = registry();
  if (!reg) return;
  if (ranges.length === 0) {
    reg.delete(name);
    return;
  }
  const Ctor = (globalThis as unknown as { Highlight: HighlightConstructor }).Highlight;
  reg.set(name, new Ctor(...ranges));
}

export function deleteHighlight(name: string): void {
  registry()?.delete(name);
}
