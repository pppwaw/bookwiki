'use client';

import './highlight.css';

import { useEffect } from 'react';
import { usePathname, useSearchParams } from 'next/navigation';
import { HighlightColors, useHighlights } from '@/lib/highlights';
import { rangesFromAnchor } from '@/lib/highlight-anchor';
import { deleteHighlight, setHighlight, supportsHighlights } from '@/lib/css-highlights';

export const DOC_ROOT_ID = 'bookwiki-doc-root';

function highlightName(color: string): string {
  return `bookwiki-hl-${color}`;
}

function clearAll(): void {
  for (const color of HighlightColors) deleteHighlight(highlightName(color));
}

/**
 * Paints persisted highlights for the current page using the CSS Custom
 * Highlight API. Re-paints after async content (KaTeX/Mermaid) settles via a
 * MutationObserver, since the API does not track DOM changes on its own.
 */
export function HighlightLayer() {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { highlights, hydrated } = useHighlights();
  const focusId = searchParams.get('hl');

  useEffect(() => {
    if (!hydrated || typeof window === 'undefined' || !supportsHighlights()) return;
    const root = document.getElementById(DOC_ROOT_ID);
    if (!root) return;

    const pageHighlights = highlights.filter((highlight) => highlight.pagePath === pathname);
    let raf = 0;
    let cancelled = false;

    const paint = () => {
      if (cancelled) return;
      const byColor = new Map<string, Range[]>();
      for (const highlight of pageHighlights) {
        const ranges = rangesFromAnchor(root, highlight);
        if (ranges.length === 0) continue;
        const list = byColor.get(highlight.color) ?? [];
        list.push(...ranges);
        byColor.set(highlight.color, list);
      }
      for (const color of HighlightColors) {
        setHighlight(highlightName(color), byColor.get(color) ?? []);
      }
    };

    const schedule = () => {
      cancelAnimationFrame(raf);
      raf = requestAnimationFrame(() => requestAnimationFrame(paint));
    };

    schedule();
    const observer = new MutationObserver(schedule);
    observer.observe(root, { subtree: true, childList: true, characterData: true });

    return () => {
      cancelled = true;
      cancelAnimationFrame(raf);
      observer.disconnect();
      clearAll();
    };
  }, [highlights, hydrated, pathname]);

  // Deep-link from the review page: scroll to and briefly flash the target.
  useEffect(() => {
    if (!hydrated || !focusId || typeof window === 'undefined') return;
    const target = highlights.find((highlight) => highlight.id === focusId);
    if (!target || target.pagePath !== pathname) return;
    const root = document.getElementById(DOC_ROOT_ID);
    if (!root) return;

    let attempts = 0;
    let raf = 0;
    const tryFocus = () => {
      const ranges = rangesFromAnchor(root, target);
      if (ranges.length > 0) {
        const rect = ranges[0].getBoundingClientRect();
        window.scrollTo({ top: window.scrollY + rect.top - window.innerHeight / 3, behavior: 'smooth' });
        const flash = highlightName('flash');
        setHighlight(flash, ranges);
        window.setTimeout(() => deleteHighlight(flash), 1600);
        // Drop the query param so a refresh doesn't re-trigger the flash.
        window.history.replaceState(null, '', pathname);
        return;
      }
      if (attempts++ < 30) raf = requestAnimationFrame(tryFocus);
    };
    raf = requestAnimationFrame(tryFocus);
    return () => cancelAnimationFrame(raf);
  }, [focusId, hydrated, highlights, pathname]);

  return null;
}
