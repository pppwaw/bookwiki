import katex from 'katex';

// Shared KaTeX rendering used by both `Formula` (body math, rewritten from
// `$...$` / `$$...$$` by the `rehypeMathComponent` build plugin) and `MathText`
// (math living inside JSX attribute strings). Keeping a single renderer ensures
// identical KaTeX options everywhere, and the page already loads the matching
// stylesheet via the global `katex/dist/katex.css` import.

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

export function normalizeKatexInput(tex: string): string {
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

export function renderKatexToString(tex: string, displayMode: boolean): string {
  return katex.renderToString(normalizeKatexInput(tex), {
    throwOnError: false,
    strict: false,
    output: 'html',
    displayMode,
  });
}
