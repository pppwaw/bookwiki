'use client';

import type { Graph as G6Graph, IElementEvent } from '@antv/g6';
import { useRouter } from 'next/navigation';
import { useEffect, useRef } from 'react';

interface RawNode {
  id: string;
  name: string;
  slug: string;
  group?: string;
  summary?: string;
  degree?: number;
}
interface RawEdge {
  source: string;
  target: string;
  weight?: number;
}
interface RawGraph {
  nodes: RawNode[];
  edges: RawEdge[];
}

const FONT = 13;
const PILL_H = FONT + 14;

// Stable colour per group (chapter) via a hashed hue.
function colorForGroup(group: string | undefined): string {
  if (!group) return 'hsl(220 9% 52%)';
  let hash = 0;
  for (let i = 0; i < group.length; i += 1) {
    hash = (hash * 31 + group.charCodeAt(i)) % 360;
  }
  return `hsl(${hash} 64% 50%)`;
}

let measureCanvas: HTMLCanvasElement | null = null;
function measureWidth(text: string): number {
  if (!measureCanvas) measureCanvas = document.createElement('canvas');
  const ctx = measureCanvas.getContext('2d');
  if (!ctx) return text.length * 8;
  ctx.font = `600 ${FONT}px ui-sans-serif, system-ui, sans-serif`;
  return ctx.measureText(text).width;
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

type KatexLike = {
  renderToString: (tex: string, options?: Record<string, unknown>) => string;
};

// Render math spans with KaTeX; escape everything else. Handles all delimiters
// the LLM emits: `$...$`, `$$...$$`, `\(...\)` (inline) and `\[...\]` (display).
// The site already imports `katex/dist/katex.css` globally (app/layout.tsx).
function renderSummaryHtml(katex: KatexLike, text: string): string {
  const re = /(\$\$[\s\S]+?\$\$|\$[^$\n]+?\$|\\\([\s\S]+?\\\)|\\\[[\s\S]+?\\\])/g;
  return text
    .split(re)
    .map((seg) => {
      let tex: string | null = null;
      let display = false;
      if (seg.length > 4 && seg.startsWith('$$') && seg.endsWith('$$')) {
        tex = seg.slice(2, -2);
        display = true;
      } else if (seg.length > 2 && seg.startsWith('$') && seg.endsWith('$')) {
        tex = seg.slice(1, -1);
      } else if (seg.startsWith('\\(') && seg.endsWith('\\)')) {
        tex = seg.slice(2, -2);
      } else if (seg.startsWith('\\[') && seg.endsWith('\\]')) {
        tex = seg.slice(2, -2);
        display = true;
      }
      if (tex !== null) {
        try {
          return katex.renderToString(tex, { throwOnError: false, displayMode: display });
        } catch {
          return escapeHtml(seg);
        }
      }
      return escapeHtml(seg);
    })
    .join('');
}

export function ConceptGraph({ height = 560 }: { height?: number }) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const graphRef = useRef<G6Graph | null>(null);
  const router = useRouter();

  useEffect(() => {
    let disposed = false;
    const containerEl = containerRef.current;

    const run = async () => {
      const res = await fetch('/concept-graph.json');
      if (!res.ok) return;
      const raw = (await res.json()) as RawGraph;
      if (disposed || !containerRef.current || graphRef.current || !raw.nodes?.length) {
        return;
      }

      // Highest-degree concept anchors the centre.
      let hub = raw.nodes[0];
      for (const n of raw.nodes) {
        if ((n.degree ?? 0) > (hub.degree ?? 0)) hub = n;
      }

      // Pin the hub at the canvas centre (d3-force reads fx/fy) so the layout
      // builds around it: the most-connected concept is the default focus and no
      // camera move is needed on open.
      const cx = (containerRef.current?.clientWidth ?? 720) / 2;
      const cy = height / 2;

      type G6NodeInput = {
        id: string;
        data: Record<string, unknown>;
        style: { size: [number, number]; fill: string; stroke: string };
        fx?: number;
        fy?: number;
      };
      const nodes: G6NodeInput[] = raw.nodes.map((n) => {
        const w = measureWidth(n.name) + 26;
        const r = Math.max(PILL_H / 2, w * 0.42) + 4; // collision radius
        const fill = colorForGroup(n.group);
        const node: G6NodeInput = {
          id: n.id,
          data: {
            name: n.name,
            slug: n.slug,
            summary: n.summary ?? '',
            degree: n.degree ?? 0,
            r,
          },
          style: { size: [w, PILL_H] as [number, number], fill, stroke: fill },
        };
        if (n.id === hub.id) {
          node.fx = cx;
          node.fy = cy;
        }
        return node;
      });
      const edges = raw.edges.map((e, i) => ({
        id: `e-${i}`,
        source: e.source,
        target: e.target,
      }));

      const [{ Graph }, katexMod] = await Promise.all([
        import('@antv/g6'),
        import('katex'),
      ]);
      const katex = katexMod.default as KatexLike;
      if (disposed || !containerRef.current) return;

      const graph = new Graph({
        container: containerRef.current,
        autoResize: true,
        data: { nodes, edges },
        node: {
          type: 'rect',
          style: {
            radius: PILL_H / 2,
            lineWidth: 1,
            labelText: (d) => String((d.data?.name as string | undefined) ?? d.id),
            labelPlacement: 'center',
            labelFill: '#ffffff',
            labelFontSize: 12,
            labelFontWeight: 600,
          },
        },
        edge: {
          style: { stroke: '#94a3b8', strokeOpacity: 0.3, lineWidth: 1 },
        },
        layout: {
          type: 'd3-force',
          // Converge fast so render() resolves (and the card reveals) within ~1s
          // instead of animating for several seconds.
          alphaDecay: 0.12,
          alphaMin: 0.05,
          link: { distance: 50, strength: 0.35 },
          manyBody: { strength: -45 },
          collide: {
            // @antv/layout exposes the original node at `_original`.
            radius: (d: { _original?: { data?: { r?: number } } }) =>
              d._original?.data?.r ?? 30,
            strength: 0.95,
            iterations: 4,
          },
          x: { strength: 0.07 },
          y: { strength: 0.07 },
        },
        behaviors: ['drag-element-force', 'zoom-canvas', 'drag-canvas'],
        plugins: [
          {
            type: 'tooltip',
            trigger: 'hover',
            getContent: (_evt: unknown, items: Array<{ id?: string; data?: Record<string, unknown> }>) => {
              const d = items?.[0];
              const name = (d?.data?.name as string | undefined) ?? d?.id ?? '';
              const summary = (d?.data?.summary as string | undefined) ?? '';
              return `<div style="max-width:260px;padding:6px 8px;font-size:13px;line-height:1.5">
                <strong>${escapeHtml(name)}</strong>${
                  summary
                    ? `<br/><span style="color:#888;font-size:12px">${renderSummaryHtml(katex, summary)}</span>`
                    : ''
                }</div>`;
            },
          },
        ],
      });

      graph.on<IElementEvent>('node:click', (evt) => {
        const id = evt.target?.id;
        if (id) router.push(`/docs/concepts/${id}`);
      });

      // Reveal the (initially opacity-0) card once the layout has mostly
      // settled — on render() completion, or a 1.2s fallback if it runs long —
      // so the entry "bounce" stays hidden behind a fade-in while the simulation
      // stays live for elastic drag, and the card never sits blank for long.
      const reveal = () => {
        if (!disposed && containerEl) containerEl.style.opacity = '1';
      };
      setTimeout(reveal, 1200);

      await graph.render();
      if (disposed) {
        graph.destroy();
        return;
      }
      graphRef.current = graph;
      // Hub is pinned at the canvas centre (fx/fy), so the default view shows it
      // centred — no camera move needed.
      reveal();
    };

    run().catch(() => {
      /* missing/invalid graph file: render nothing rather than fabricate data */
    });

    return () => {
      disposed = true;
      if (containerEl) containerEl.style.opacity = '0';
      try {
        graphRef.current?.destroy();
      } catch {
        /* already destroyed */
      }
      graphRef.current = null;
    };
  }, [router, height]);

  return (
    <div
      ref={containerRef}
      style={{
        position: 'relative',
        width: '100%',
        height,
        borderRadius: 16,
        overflow: 'hidden',
        border: '1px solid color-mix(in srgb, currentColor 12%, transparent)',
        background:
          'radial-gradient(120% 120% at 50% 0%, color-mix(in srgb, currentColor 4%, transparent), transparent 70%)',
        opacity: 0,
        transition: 'opacity 0.5s ease',
      }}
    />
  );
}

export default ConceptGraph;
