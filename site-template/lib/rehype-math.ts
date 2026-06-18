// rehype plugin: rewrite remark-math output into a single PLAIN element per
// formula carrying only the raw TeX, instead of letting `rehype-katex` expand
// each `$...$` / `$$...$$` into KaTeX's large span tree at build time.
//
// remark-math + remark-rehype emit:
//   inline  `$x$`    -> <code class="language-math math-inline">x</code>
//   display `$$x$$`  -> <pre><code class="language-math math-display">x</code></pre>
//
// We replace those with one plain HTML element holding the raw TeX:
//   inline  -> <span class="math math-inline katex-src">x</span>
//   display -> <div class="math math-display katex-src">x</div>
//
// Why plain `<span>`/`<div>` and NOT a custom `<Formula>` component: fumadocs
// renders MDX content in several contexts (page body via the components
// provider, but also the TOC `title` and search excerpts as standalone JSX that
// reference components by bare name). A custom component compiles to a bare
// `_jsx(Formula, ...)` reference in the TOC export, which is evaluated at module
// load with no provider in scope -> `ReferenceError: Formula is not defined`.
// Lowercase HTML tags compile to `_jsx("span", ...)` and are always valid in
// every context. The raw TeX text is rendered to KaTeX on the client by the
// `KatexClient` effect, which scans for the `katex-src` marker class.
//
// Net effect: the compiled MDX module shrinks from thousands of KaTeX spans to a
// single element per formula (fixes the webpack compile-phase memory blow-up)
// and React only tracks one element per formula (fixes the `next dev`
// fake-stack quadratic), while staying safe in every render context.
//
// This must run BEFORE the default rehype plugins (Shiki) so the syntax
// highlighter never sees the unknown `language-math` fence.

/* eslint-disable @typescript-eslint/no-explicit-any */

function classNames(node: any): string[] {
  const value = node?.properties?.className;
  if (Array.isArray(value)) return value as string[];
  if (typeof value === 'string') return value.split(/\s+/);
  return [];
}

function textContent(node: any): string {
  if (node?.type === 'text') return node.value as string;
  if (Array.isArray(node?.children)) return node.children.map(textContent).join('');
  return '';
}

function isMathCode(node: any): boolean {
  return (
    node?.type === 'element' &&
    node.tagName === 'code' &&
    classNames(node).includes('language-math')
  );
}

function mathElement(tex: string, display: boolean): any {
  return {
    type: 'element',
    tagName: display ? 'div' : 'span',
    properties: {
      className: ['math', display ? 'math-display' : 'math-inline', 'katex-src'],
    },
    children: [{ type: 'text', value: tex }],
  };
}

export function rehypeMath() {
  return (tree: any) => {
    const walk = (node: any): void => {
      if (!node || !Array.isArray(node.children)) return;

      const next: any[] = [];
      for (const child of node.children) {
        // display: <pre><code class="language-math math-display">…</code></pre>
        if (
          child?.type === 'element' &&
          child.tagName === 'pre' &&
          child.children?.length === 1 &&
          isMathCode(child.children[0])
        ) {
          next.push(mathElement(textContent(child.children[0]), true));
          continue;
        }

        // inline: <code class="language-math math-inline">…</code>
        if (isMathCode(child)) {
          next.push(mathElement(textContent(child), classNames(child).includes('math-display')));
          continue;
        }

        walk(child);
        next.push(child);
      }
      node.children = next;
    };

    walk(tree);
  };
}
