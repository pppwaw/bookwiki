'use client';

/**
 * Client-side highlight store backed by localStorage, namespaced per book via
 * `bookKey`. A module-level store with `useSyncExternalStore` keeps the
 * separate HighlightLayer and HighlightToolbar components in sync within the
 * same tab (the native `storage` event only fires across tabs).
 */
import { useSyncExternalStore } from 'react';
import { bookKey } from './book-meta';
import type { TextAnchor } from './highlight-anchor';

export const HighlightColors = ['amber', 'rose', 'sky', 'emerald'] as const;
export type HighlightColor = (typeof HighlightColors)[number];

export type Highlight = TextAnchor & {
  id: string;
  /** Route path the highlight lives on, e.g. `/docs/chapters/ch01-...`. */
  pagePath: string;
  /** Human label for the page, for grouping on the review page. */
  pageTitle: string;
  color: HighlightColor;
  /** Display text with formulas as `$tex$`, for re-rendering math on the review page. */
  quoteRich?: string;
  note?: string;
  createdAt: number;
  updatedAt: number;
};

const STORAGE_KEY = bookKey('highlights:v1');

const EMPTY: Highlight[] = [];
let cache: Highlight[] = EMPTY;
let hydrated = false;
const listeners = new Set<() => void>();

function emit(): void {
  for (const listener of listeners) listener();
}

function persist(): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify({ highlights: cache }));
  } catch {
    // Best-effort; ignore quota/serialization errors.
  }
}

function load(): Highlight[] {
  if (typeof window === 'undefined') return EMPTY;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return EMPTY;
    const parsed = JSON.parse(raw) as { highlights?: unknown };
    if (!Array.isArray(parsed.highlights)) return EMPTY;
    return parsed.highlights.filter(isHighlight);
  } catch {
    return EMPTY;
  }
}

function isHighlight(value: unknown): value is Highlight {
  if (typeof value !== 'object' || value === null) return false;
  const candidate = value as Partial<Highlight>;
  return (
    typeof candidate.id === 'string' &&
    typeof candidate.quote === 'string' &&
    typeof candidate.pagePath === 'string' &&
    typeof candidate.color === 'string'
  );
}

function ensureHydrated(): void {
  if (hydrated || typeof window === 'undefined') return;
  hydrated = true;
  cache = load();
  emit();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  ensureHydrated();
  return () => {
    listeners.delete(listener);
  };
}

function newId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  return `h_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 10)}`;
}

export function getHighlights(): Highlight[] {
  return cache;
}

export function isHydrated(): boolean {
  return hydrated;
}

export type NewHighlight = TextAnchor & {
  pagePath: string;
  pageTitle: string;
  color: HighlightColor;
  quoteRich?: string;
  note?: string;
};

export function addHighlight(input: NewHighlight): Highlight {
  const now = Date.now();
  const highlight: Highlight = { ...input, id: newId(), createdAt: now, updatedAt: now };
  cache = [highlight, ...cache];
  persist();
  emit();
  return highlight;
}

export function updateHighlight(
  id: string,
  patch: Partial<Pick<Highlight, 'color' | 'note'>>,
): void {
  let changed = false;
  cache = cache.map((highlight) => {
    if (highlight.id !== id) return highlight;
    changed = true;
    return { ...highlight, ...patch, updatedAt: Date.now() };
  });
  if (changed) {
    persist();
    emit();
  }
}

export function removeHighlight(id: string): void {
  const next = cache.filter((highlight) => highlight.id !== id);
  if (next.length === cache.length) return;
  cache = next;
  persist();
  emit();
}

/** React binding: re-renders on any highlight change. SSR-safe (empty until hydrated). */
export function useHighlights(): { highlights: Highlight[]; hydrated: boolean } {
  const highlights = useSyncExternalStore(
    subscribe,
    getHighlights,
    () => EMPTY,
  );
  return { highlights, hydrated };
}
