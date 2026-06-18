import { renderKatexToString } from '@/lib/katex';

// Render runtime string props (e.g. citation `quote`) that contain `$...$` /
// `$$...$$` math. These strings live inside JSX attribute expressions, so the
// build-time remark-math pipeline never sees them — math must be rendered here,
// at runtime, with the same shared KaTeX renderer the rest of the site uses.
const TOKEN_RE = /(\$\$[\s\S]*?\$\$|\$[^$\n]*\$)/g;

export function MathText({ text }: { text?: string }) {
  if (!text) return null;

  const segments = text.split(TOKEN_RE);
  return (
    <>
      {segments.map((segment, index) => {
        if (!segment) return null;

        if (segment.length >= 4 && segment.startsWith('$$') && segment.endsWith('$$')) {
          return (
            <span
              key={index}
              className="math math-display"
              dangerouslySetInnerHTML={{ __html: renderKatexToString(segment.slice(2, -2).trim(), true) }}
            />
          );
        }

        if (segment.length >= 2 && segment.startsWith('$') && segment.endsWith('$')) {
          return (
            <span
              key={index}
              className="math math-inline"
              dangerouslySetInnerHTML={{ __html: renderKatexToString(segment.slice(1, -1).trim(), false) }}
            />
          );
        }

        return <span key={index}>{segment}</span>;
      })}
    </>
  );
}
