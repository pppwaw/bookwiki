// Shared citation parsing for chat output, used by both the markdown renderer
// (components/markdown.tsx) and the chat API source extraction
// (app/api/chat/route.ts) so both sides agree on what counts as a citation.
//
// A citation group is either:
//   - a caret bracket `[^ … ]` not immediately followed by `:` — one or more
//     comma-separated entries, each optionally prefixed with `^` and/or `page:`
//   - a bracketed source_ref `[ …-pN ]`
//
// An entry is a PAGE citation when its payload contains `/` (page slugs always
// contain `/`, e.g. `concepts/Self-Inductance`; source_refs never do, e.g.
// `Chapter-1---Basic-concepts-HX-p003`). Otherwise it is a source_ref. We parse
// whatever the model emits from the tool's `page` slug rather than forcing a
// synthetic citation format: a bare `[^<slug>]`, an optional `page:` prefix, a
// trailing `#…` fragment, and several refs packed into one bracket are all
// tolerated and normalized to the underlying page slug.

export type CitationToken =
  | { kind: 'source'; ref: string }
  | { kind: 'page'; slug: string };

// Fresh `/g` regex per call so concurrent `.test()`/`matchAll()` consumers never
// share a mutable `lastIndex`.
export function citationGroupRegex(): RegExp {
  return /\[\^([^\]]+)\](?!:)|\[([A-Za-z0-9_.:-]+-p\d+[A-Za-z0-9_.:-]*)\](?!\()/g;
}

const sourceTokenPattern = /^[A-Za-z0-9_.:-]+$/;
const pageSlugPattern = /^[A-Za-z0-9_.%/-]+$/;
const pagePrefix = 'page:';

/**
 * Classify the entries of one matched citation group. Returns null when any
 * comma-separated entry is not a valid citation so callers can leave ambiguous
 * text (e.g. `[^note this]`, or a `#chunk-003` id) untouched as literal text.
 */
export function tokensFromMatch(match: RegExpMatchArray): CitationToken[] | null {
  if (match[1] !== undefined) return parseCaretTokens(match[1]);
  if (match[2] !== undefined) return [{ kind: 'source', ref: match[2] }];
  return null;
}

function parseCaretTokens(inner: string): CitationToken[] | null {
  const entries = inner
    .split(',')
    .map((entry) => entry.trim())
    .filter(Boolean);
  if (entries.length === 0) return null;

  const tokens: CitationToken[] = [];
  for (const entry of entries) {
    const withoutCaret = entry.replace(/^\^/, '').trim();
    const isPagePrefixed = withoutCaret.startsWith(pagePrefix);
    const payload = (isPagePrefixed ? withoutCaret.slice(pagePrefix.length) : withoutCaret).trim();
    if (!payload) return null;

    if (isPagePrefixed || payload.includes('/')) {
      // Normalize to the page slug: drop any `#…` fragment (e.g. a `#chunk-003`
      // tail the model may copy) and a trailing slash.
      const slug = payload.split('#')[0].replace(/\/+$/, '');
      if (!slug || !pageSlugPattern.test(slug)) return null;
      tokens.push({ kind: 'page', slug });
    } else {
      if (!sourceTokenPattern.test(payload)) return null;
      tokens.push({ kind: 'source', ref: payload });
    }
  }
  return tokens;
}
