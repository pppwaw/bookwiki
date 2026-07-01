// Slug decoding shared by client display helpers and server-side lookups.
//
// fumadocs percent-encodes every non-ASCII slug segment via `encodeURI`
// (see fumadocs-core source/plugins/slugs.ts), and Next.js `usePathname()`
// returns that already-encoded pathname. So a Chinese page `概念/感应` travels
// as `%E6%A6%82%E5%BF%B5/%E6%84%9F%E5%BA%94`. Anything rendering a slug as
// human-facing text must decode it back, or the UI shows raw `%E6%84…`.
//
// This module has no server-only imports so it is safe to use from client
// components (`'use client'`) as well as server code.

/** Decode one segment, returning it unchanged if it is not valid percent-encoding. */
export function safeDecodeURIComponent(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/**
 * Decode a `/`-separated slug segment-by-segment. Splitting first keeps the
 * `/` separators intact and matches how fumadocs encodes each segment on its
 * own (a whole-string decode would also be fine here, but per-segment mirrors
 * the encoding and is robust to a literal `%2F` inside a segment).
 */
export function decodeSlug(slug: string): string {
  return slug.split('/').map(safeDecodeURIComponent).join('/');
}
