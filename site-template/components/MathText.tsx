import katex from 'katex';

// Render runtime string props (e.g. citation `quote`) that contain `$...$` /
// `$$...$$` math. These strings live inside JSX attribute expressions, so the
// build-time remark-math + rehype-katex pipeline never sees them — math must be
// rendered here, at runtime, with the same KaTeX engine the page already loads
// (`katex` dep + global `katex/dist/katex.css`).
const TOKEN_RE = /(\$\$[\s\S]*?\$\$|\$[^$\n]*\$)/g;
const KATEX_TEXT_MODE_DIGITS: Record<string, string> = {
  '①': '1',
  '②': '2',
  '③': '3',
  '④': '4',
  '⑤': '5',
  '⑥': '6',
  '⑦': '7',
  '⑧': '8',
  '⑨': '9',
  '⑩': '10',
};

function renderMath(tex: string, displayMode: boolean): string {
  return katex.renderToString(normalizeKatexInput(tex), {
    throwOnError: false,
    strict: false,
    output: 'html',
    displayMode,
  });
}

function normalizeKatexInput(tex: string): string {
  return tex
    .replace(/[①②③④⑤⑥⑦⑧⑨⑩]/g, (value) => KATEX_TEXT_MODE_DIGITS[value] ?? value)
    .replace(/\\text\{([^{}]*)\}/g, (_match, text: string) => {
      if (!text.includes('θ')) return `\\text{${text}}`;

      return text
        .split('θ')
        .map((part) => (part ? `\\text{${part}}` : ''))
        .join('\\theta');
    });
}

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
              dangerouslySetInnerHTML={{ __html: renderMath(segment.slice(2, -2).trim(), true) }}
            />
          );
        }

        if (segment.length >= 2 && segment.startsWith('$') && segment.endsWith('$')) {
          return (
            <span
              key={index}
              className="math math-inline"
              dangerouslySetInnerHTML={{ __html: renderMath(segment.slice(1, -1).trim(), false) }}
            />
          );
        }

        return <span key={index}>{segment}</span>;
      })}
    </>
  );
}
