import { toJsxRuntime } from 'hast-util-to-jsx-runtime';
import type { Nodes } from 'hast';
import type { ReactNode } from 'react';
import { Fragment, jsx, jsxs } from 'react/jsx-runtime';
import rehypeKatex from 'rehype-katex';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import remarkParse from 'remark-parse';
import remarkRehype from 'remark-rehype';
import { unified } from 'unified';

// Markdown → hast pipeline with `$…$` / `$$…$$` math. KaTeX styling comes from
// `katex/dist/katex.css` (imported in the root layout). Chunk text is already
// MDX-scaffold-free (stripped at the chunker), so only prose + math reach here.
const processor = unified()
  .use(remarkParse)
  .use(remarkGfm)
  .use(remarkMath)
  .use(remarkRehype)
  .use(rehypeKatex);

export function markdownToHast(text: string): Nodes {
  return processor.runSync(processor.parse(text)) as Nodes;
}

export function renderSearchMarkdown(text: string): ReactNode {
  return toJsxRuntime(markdownToHast(text), { Fragment, jsx, jsxs });
}
