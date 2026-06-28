'use client';

import './KatexClient.css';

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
  const nodes = new Set<HTMLElement>(root.querySelectorAll<HTMLElement>('.katex-src'));
  // querySelectorAll only finds descendants — include the root itself when it is
  // the marker (e.g. a bare inline-math span handed straight to the observer).
  if (root instanceof HTMLElement && root.classList.contains('katex-src')) nodes.add(root);
  nodes.forEach((el) => {
    const display = el.classList.contains('math-display');
    const tex = el.textContent ?? '';
    try {
      el.innerHTML = renderKatexToString(tex, display);
      // Preserve the source TeX (rendering overwrites it) so highlights can
      // reconstruct `$tex$` for the review page.
      el.dataset.tex = tex;
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

    // Robustness over the fragile "every island must remember to re-trigger us"
    // contract: watch for any `.katex-src` that enters the DOM later — exam
    // explanations revealed on submit, flipped Anki backs, quiz feedback, or any
    // future reveal — and paint it automatically. Rendering strips the
    // `katex-src` class and emits non-marker KaTeX markup, so this never loops.
    const observer = new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        mutation.addedNodes.forEach((node) => {
          if (!(node instanceof HTMLElement)) return;
          if (node.classList.contains('katex-src') || node.querySelector('.katex-src')) {
            renderPendingKatex(node);
          }
        });
      }
    });
    observer.observe(document.body, { childList: true, subtree: true });
    return () => observer.disconnect();
  }, [pathname]);

  return null;
}
