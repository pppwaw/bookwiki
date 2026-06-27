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

/** Build a TextAnchor from a live DOM selection range scoped to `root`. */
export function anchorFromRange(root: Node, range: Range): TextAnchor | null {
  const quote = range.toString();
  if (!quote.trim()) return null;

  const pre = document.createRange();
  pre.selectNodeContents(root);
  pre.setEnd(range.startContainer, range.startOffset);
  const start = pre.toString().length;
  const end = start + quote.length;

  const text = root.textContent ?? '';
  return {
    quote,
    prefix: text.slice(Math.max(0, start - CONTEXT), start),
    suffix: text.slice(end, end + CONTEXT),
    start,
    end,
  };
}

function rangeFromOffsets(root: Node, start: number, end: number): Range | null {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  let acc = 0;
  let startSet = false;
  for (let node = walker.nextNode(); node !== null; node = walker.nextNode()) {
    const len = node.textContent?.length ?? 0;
    if (!startSet && acc + len >= start) {
      range.setStart(node, start - acc);
      startSet = true;
    }
    if (startSet && acc + len >= end) {
      range.setEnd(node, end - acc);
      return range;
    }
    acc += len;
  }
  return null;
}

/** Re-resolve an anchor to a live DOM Range within `root`, or null if orphaned. */
export function rangeFromAnchor(root: Node, anchor: TextAnchor): Range | null {
  const text = root.textContent ?? '';
  const span = locateAnchor(text, anchor);
  if (!span) return null;
  return rangeFromOffsets(root, span.start, span.end);
}

/** True when a click offset falls inside an anchor's resolved span. */
export function anchorContainsOffset(haystack: string, anchor: TextAnchor, offset: number): boolean {
  const span = locateAnchor(haystack, anchor);
  return span !== null && offset >= span.start && offset < span.end;
}
