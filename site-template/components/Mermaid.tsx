'use client';

import { use, useEffect, useId, useState } from 'react';
import { useTheme } from 'next-themes';

type MermaidRender = { svg: string; bindFunctions?: (element: Element) => void };

const cache = new Map<string, Promise<unknown>>();

function cachePromise<T>(key: string, setPromise: () => Promise<T>): Promise<T> {
  const cached = cache.get(key);
  if (cached) return cached as Promise<T>;
  const promise = setPromise();
  cache.set(key, promise);
  return promise;
}

function escapeHtml(value: string): string {
  return value.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
}

/**
 * Render a Mermaid diagram from a ```mermaid code fence.
 *
 * Authors write a plain ```mermaid fenced block; `remarkMdxMermaid` rewrites it to
 * `<Mermaid chart={...} />` at build time. Rendering is client-only (mermaid touches
 * the DOM), theme-aware via next-themes, and deduplicated per (chart, theme). A diagram
 * that fails to parse degrades to its raw source instead of crashing the page.
 */
export function Mermaid({ chart }: { chart: string }) {
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  if (!mounted) return null;
  return <MermaidContent chart={chart} />;
}

function MermaidContent({ chart }: { chart: string }) {
  const id = useId();
  const { resolvedTheme } = useTheme();
  const { default: mermaid } = use(cachePromise('mermaid', () => import('mermaid')));

  mermaid.initialize({
    startOnLoad: false,
    securityLevel: 'loose',
    fontFamily: 'inherit',
    themeCSS: 'margin: 1.5rem auto 0;',
    theme: resolvedTheme === 'dark' ? 'dark' : 'default',
  });

  const renderId = `mermaid-${id.replace(/[^a-zA-Z0-9]/g, '')}`;
  const { svg, bindFunctions } = use(
    cachePromise<MermaidRender>(`${chart}-${resolvedTheme}`, () =>
      mermaid.render(renderId, chart).catch(() => ({
        svg: `<pre class="mermaid-error"><code>${escapeHtml(chart)}</code></pre>`,
      })),
    ),
  );

  return (
    <div
      className="my-4 flex justify-center [&_svg]:h-auto [&_svg]:max-w-full"
      ref={(container) => {
        if (container) bindFunctions?.(container);
      }}
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}
