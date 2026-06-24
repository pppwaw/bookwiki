'use client';

import { usePathname } from 'next/navigation';
import { useEffect } from 'react';
import { renderKatexToString } from '@/lib/katex';

// Client-side KaTeX renderer for body/heading/TOC math. The `rehypeMath` build
// plugin rewrites every `$...$` / `$$...$$` into a plain
// `<span|div class="math … katex-src">rawTeX</…>` element, so the compiled MDX
// module carries only the raw TeX (one element per formula) and stays valid in
// every render context (page body, TOC title, search). Here we find those
// markers and render KaTeX into them in the browser.
//
// Mounted once in the docs layout, so a single pass covers the article body and
// the sidebar TOC. We re-run on pathname change because fumadocs navigates
// client-side (the DOM is swapped without a full reload). Interactive MDX
// islands such as Anki cards can also call `renderPendingKatex` after they mount
// new hidden/revealed faces. Each element loses its `katex-src` class once
// rendered, so repeated passes never reprocess it, and React never clobbers the
// injected markup: it diffs fiber-to-fiber (the vdom children are the unchanged
// raw-TeX text), so our out-of-band `innerHTML` survives until the node actually
// unmounts on navigation.

export function renderPendingKatex(root: ParentNode = document) {
  const nodes = root.querySelectorAll<HTMLElement>('.katex-src');
  nodes.forEach((el) => {
    const display = el.classList.contains('math-display');
    const tex = el.textContent ?? '';
    try {
      el.innerHTML = renderKatexToString(tex, display);
    } catch {
      // Leave the raw TeX in place if KaTeX cannot parse it.
    }
    el.classList.remove('katex-src');
  });
}

export function KatexClient() {
  const pathname = usePathname();

  useEffect(() => {
    renderPendingKatex();
  }, [pathname]);

  return null;
}
