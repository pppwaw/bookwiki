/**
 * Text anchoring for highlights, modeled on the W3C Web Annotation TextQuote /
 * TextPosition selectors (the same approach Hypothesis uses).
 *
 * An anchor stores the exact quote plus surrounding context and a character
 * offset. On reload we re-find the range; minor whitespace reflow is tolerated.
 * A large content rewrite orphans the anchor (returns null) — by design the
 * highlight is not lost, it simply stops painting and stays listed on the
 * review page.
 */
import { diff_match_patch } from 'diff-match-patch';

export type TextAnchor = {
  /** Exact selected text (TextQuoteSelector.exact). */
  quote: string;
  /** Up to CONTEXT chars before the quote, for disambiguation. */
  prefix: string;
  /** Up to CONTEXT chars after the quote, for disambiguation. */
  suffix: string;
  /** Character offset of the quote start within the root's textContent. */
  start: number;
  /** Character offset of the quote end within the root's textContent. */
  end: number;
};

const CONTEXT = 32;
// diff-match-patch's bitap matcher caps patterns at Match_MaxBits (32).
const MATCH_MAX_BITS = 32;

type Span = { start: number; end: number };

// Single tuned matcher instance; the bitap params are diff-match-patch defaults.
const matcher = new diff_match_patch();

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

/**
 * Last-resort fuzzy locate via diff-match-patch's Bitap matcher — tolerates
 * word-level edits near the quote that whitespace normalization cannot.
 * Patterns are capped at 32 chars, so long quotes are anchored by matching
 * their head (start) and tail (end) separately.
 */
function findFuzzy(haystack: string, anchor: TextAnchor): Span | null {
  if (!anchor.quote) return null;
  const head = anchor.quote.slice(0, MATCH_MAX_BITS);
  const start = matcher.match_main(haystack, head, clamp(anchor.start, 0, haystack.length));
  if (start === -1) return null;

  let end: number;
  if (anchor.quote.length <= MATCH_MAX_BITS) {
    end = start + anchor.quote.length;
  } else {
    const tail = anchor.quote.slice(-MATCH_MAX_BITS);
    const tailMatch = matcher.match_main(haystack, tail, clamp(anchor.end - MATCH_MAX_BITS, 0, haystack.length));
    end = tailMatch === -1 ? start + anchor.quote.length : tailMatch + MATCH_MAX_BITS;
  }
  end = Math.min(haystack.length, Math.max(end, start + 1));
  return end > start ? { start, end } : null;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Score a candidate by how well the surrounding text matches the stored context. */
function contextScore(haystack: string, span: Span, anchor: TextAnchor): number {
  const before = haystack.slice(Math.max(0, span.start - CONTEXT), span.start);
  const after = haystack.slice(span.end, span.end + CONTEXT);
  return commonSuffixLength(before, anchor.prefix) + commonPrefixLength(after, anchor.suffix);
}

function commonSuffixLength(a: string, b: string): number {
  let i = 0;
  while (i < a.length && i < b.length && a[a.length - 1 - i] === b[b.length - 1 - i]) i++;
  return i;
}

function commonPrefixLength(a: string, b: string): number {
  let i = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) i++;
  return i;
}

/** Pick the candidate with the best context match; tie-break by nearest to original offset. */
function bestCandidate(haystack: string, spans: Span[], anchor: TextAnchor): Span | null {
  if (spans.length === 0) return null;
  if (spans.length === 1) return spans[0];
  let best = spans[0];
  let bestScore = -1;
  for (const span of spans) {
    const score = contextScore(haystack, span, anchor);
    if (
      score > bestScore ||
      (score === bestScore && Math.abs(span.start - anchor.start) < Math.abs(best.start - anchor.start))
    ) {
      best = span;
      bestScore = score;
    }
  }
  return best;
}

function findExact(haystack: string, quote: string): Span[] {
  const spans: Span[] = [];
  if (!quote) return spans;
  let from = 0;
  for (;;) {
    const index = haystack.indexOf(quote, from);
    if (index === -1) break;
    spans.push({ start: index, end: index + quote.length });
    from = index + 1;
  }
  return spans;
}

/** Whitespace-tolerant search: collapse runs of whitespace so reflow still matches. */
function findWhitespaceTolerant(haystack: string, quote: string): Span[] {
  const trimmed = quote.trim();
  if (!trimmed) return [];
  const pattern = escapeRegExp(trimmed).replace(/\s+/g, '\\s+');
  const spans: Span[] = [];
  const regex = new RegExp(pattern, 'g');
  for (let match = regex.exec(haystack); match !== null; match = regex.exec(haystack)) {
    spans.push({ start: match.index, end: match.index + match[0].length });
    if (match.index === regex.lastIndex) regex.lastIndex++;
  }
  return spans;
}

/**
 * Locate an anchor within `haystack` (a root's textContent). Pure and testable.
 * Tries, in order: exact offset → exact substring → whitespace-tolerant match.
 */
export function locateAnchor(haystack: string, anchor: TextAnchor): Span | null {
  if (
    anchor.quote &&
    anchor.end <= haystack.length &&
    haystack.slice(anchor.start, anchor.end) === anchor.quote
  ) {
    return { start: anchor.start, end: anchor.end };
  }

  const exact = bestCandidate(haystack, findExact(haystack, anchor.quote), anchor);
  if (exact) return exact;

  const whitespace = bestCandidate(haystack, findWhitespaceTolerant(haystack, anchor.quote), anchor);
  if (whitespace) return whitespace;

  return findFuzzy(haystack, anchor);
}

function isInHiddenMath(node: Node): boolean {
  let el: Element | null = node.nodeType === 1 ? (node as Element) : node.parentElement;
  while (el) {
    if (el.classList?.contains('katex-mathml')) return true;
    el = el.parentElement;
  }
  return false;
}

/**
 * Text nodes under `root`, skipping only KaTeX's hidden MathML mirror
 * (`.katex-mathml`). The visible math (`.katex-html`) is kept, so highlights can
 * span formulas and the formula still renders normally (the DOM is untouched).
 */
function filteredTextNodes(root: Node): Text[] {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) => (isInHiddenMath(node) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  const nodes: Text[] = [];
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    nodes.push(node as Text);
  }
  return nodes;
}

/**
 * The root's text in the coordinate space every anchor uses. KaTeX renders each
 * formula twice (hidden MathML + visible HTML); we drop the hidden mirror so a
 * quote contains the formula once instead of a duplicated garble.
 */
export function getLogicalText(root: Node): string {
  let text = '';
  for (const node of filteredTextNodes(root)) text += node.data;
  return text;
}

/** Logical (formula-excluded) offset of a DOM point within `root`. */
export function logicalOffsetOfPoint(root: Node, container: Node, offset: number): number {
  const pre = document.createRange();
  pre.selectNodeContents(root);
  try {
    pre.setEnd(container, offset);
  } catch {
    return 0;
  }
  let total = 0;
  for (const node of filteredTextNodes(root)) {
    let startCmp: number;
    try {
      startCmp = pre.comparePoint(node, 0);
    } catch {
      continue;
    }
    if (startCmp > 0) break; // node begins after the point (document order)
    const len = node.data.length;
    let endCmp: number;
    try {
      endCmp = pre.comparePoint(node, len);
    } catch {
      endCmp = 1;
    }
    total += endCmp <= 0 ? len : node === container ? offset : len;
  }
  return total;
}

/** Build a TextAnchor from a live selection range, excluding any formula text. */
export function anchorFromRange(root: Node, range: Range): TextAnchor | null {
  const text = getLogicalText(root);
  const start = logicalOffsetOfPoint(root, range.startContainer, range.startOffset);
  const end = logicalOffsetOfPoint(root, range.endContainer, range.endOffset);
  if (end <= start) return null;
  const quote = text.slice(start, end);
  if (!quote.trim()) return null;
  return {
    quote,
    prefix: text.slice(Math.max(0, start - CONTEXT), start),
    suffix: text.slice(end, end + CONTEXT),
    start,
    end,
  };
}

/**
 * Build a display string for the selection with formulas as `$tex$` / `$$tex$$`
 * (read from KaTeX's stored LaTeX annotation), so the review page can re-render
 * the math with <MathText>. Plain prose is kept verbatim. Falls back to the
 * visible text when a formula's annotation is unavailable (e.g. partial select).
 */
export function richQuoteFromRange(range: Range): string {
  const fragment = range.cloneContents();
  let out = '';
  const walk = (node: Node): void => {
    if (node.nodeType === Node.TEXT_NODE) {
      out += (node as Text).data;
      return;
    }
    if (node.nodeType !== Node.ELEMENT_NODE) return;
    const el = node as Element;
    // Body math: KatexClient renders `.math.katex-src` and preserves the source
    // TeX in `data-tex` (see KatexClient.renderPendingKatex).
    const dataTex = el.getAttribute?.('data-tex');
    if (dataTex != null) {
      const tex = dataTex.trim();
      out += tex
        ? el.classList.contains('math-display')
          ? `$$${tex}$$`
          : `$${tex}$`
        : (el.textContent ?? '');
      return; // never descend into a formula
    }
    // Fallback: math rendered with a MathML annotation (e.g. <MathText>).
    if (el.classList?.contains('katex')) {
      const annotation = el.querySelector('annotation[encoding="application/x-tex"]');
      const tex = (annotation?.textContent ?? '').trim();
      out += tex
        ? el.closest?.('.katex-display')
          ? `$$${tex}$$`
          : `$${tex}$`
        : (el.textContent ?? '');
      return;
    }
    for (const child of el.childNodes) walk(child);
  };
  for (const child of fragment.childNodes) walk(child);
  return out.trim();
}

/**
 * Resolve a logical span to a single contiguous DOM Range. Spanning a formula
 * is intentional — the highlight paints over the rendered math too, and because
 * the API never mutates the DOM, the formula keeps rendering normally.
 */
function rangeFromLogicalSpan(root: Node, start: number, end: number): Range | null {
  let acc = 0;
  let startNode: Text | undefined;
  let startOffset = 0;
  let endNode: Text | undefined;
  let endOffset = 0;
  for (const node of filteredTextNodes(root)) {
    const len = node.data.length;
    if (startNode === undefined && acc + len >= start) {
      startNode = node;
      startOffset = start - acc;
    }
    if (startNode !== undefined && acc + len >= end) {
      endNode = node;
      endOffset = end - acc;
      break;
    }
    acc += len;
  }
  if (!startNode || !endNode) return null;
  const range = document.createRange();
  range.setStart(startNode, startOffset);
  range.setEnd(endNode, endOffset);
  return range;
}

/** Re-resolve an anchor to live DOM ranges within `root` (empty if orphaned). */
export function rangesFromAnchor(root: Node, anchor: TextAnchor): Range[] {
  const span = locateAnchor(getLogicalText(root), anchor);
  if (!span) return [];
  const range = rangeFromLogicalSpan(root, span.start, span.end);
  return range ? [range] : [];
}

/** True when a logical offset falls inside an anchor's resolved span. */
export function anchorContainsOffset(haystack: string, anchor: TextAnchor, offset: number): boolean {
  const span = locateAnchor(haystack, anchor);
  return span !== null && offset >= span.start && offset < span.end;
}
