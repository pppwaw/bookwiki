/**
 * Book identity for the materialized site.
 *
 * Every book is materialized into its own `books/<id>/site/` but `next start`
 * serves them on the same `localhost:<port>` origin, and localStorage is keyed
 * by origin — so two books would otherwise share client state. `scripts/site.py`
 * injects `NEXT_PUBLIC_BOOK_ID` into `.env.local`; all client-side storage keys
 * MUST be namespaced through `bookKey()` so books never collide.
 */
export const bookId: string = process.env.NEXT_PUBLIC_BOOK_ID || 'default';

/** Namespace a localStorage key under the current book, e.g. `bookwiki:calculus:highlights:v1`. */
export function bookKey(name: string): string {
  return `bookwiki:${bookId}:${name}`;
}
