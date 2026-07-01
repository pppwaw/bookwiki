'use client';

import { remark } from 'remark';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import remarkRehype from 'remark-rehype';
import { toJsxRuntime } from 'hast-util-to-jsx-runtime';
import {
  Children,
  type ComponentProps,
  type ReactElement,
  type ReactNode,
  Suspense,
  use,
  useDeferredValue,
} from 'react';
import { Fragment, jsx, jsxs } from 'react/jsx-runtime';
import { DynamicCodeBlock } from 'fumadocs-ui/components/dynamic-codeblock';
import defaultMdxComponents from 'fumadocs-ui/mdx';
import { visit } from 'unist-util-visit';
import type { Element, ElementContent, Root, RootContent } from 'hast';
import { SourceRef } from './SourceRef';
import { citationGroupRegex, tokensFromMatch } from '@/lib/citations';
import { renderKatexToString } from '@/lib/katex';
import { safeDecodeURIComponent } from '@/lib/slug';

export interface Processor {
  process: (content: string, options?: { inline?: boolean }) => Promise<ReactNode>;
}

export function rehypeWrapWords() {
  return (tree: Root) => {
    visit(tree, ['text', 'element'], (node, index, parent) => {
      if (node.type === 'element' && shouldSkipWrap(node)) return 'skip';
      if (node.type !== 'text' || !parent || index === undefined) return;

      const words = node.value.split(/(?=\s)/);
      const newNodes: ElementContent[] = words.flatMap((word) => {
        if (word.length === 0) return [];

        return {
          type: 'element',
          tagName: 'span',
          properties: {
            class: 'animate-fd-fade-in',
          },
          children: [{ type: 'text', value: word }],
        };
      });

      Object.assign(node, {
        type: 'element',
        tagName: 'span',
        properties: {},
        children: newNodes,
      } satisfies RootContent);
      return 'skip';
    });
  };
}

export function rehypeSourceRefs() {
  return (tree: Root) => {
    visit(tree, 'text', (node, index, parent) => {
      if (parent === undefined || index === undefined) return;

      const pattern = citationGroupRegex();
      if (!pattern.test(node.value)) return;
      pattern.lastIndex = 0;

      const nodes: ElementContent[] = [];
      let lastIndex = 0;

      for (const match of node.value.matchAll(citationGroupRegex())) {
        const tokens = tokensFromMatch(match);
        if (!tokens) continue; // ambiguous bracket — leave it as literal text

        const start = match.index ?? 0;
        if (start > lastIndex) {
          nodes.push({ type: 'text', value: node.value.slice(lastIndex, start) });
        }

        tokens.forEach((token, tokenIndex) => {
          if (tokenIndex > 0) nodes.push({ type: 'text', value: ' ' });
          nodes.push(
            token.kind === 'page'
              ? {
                  type: 'element',
                  tagName: 'SourceRef',
                  properties: { id: pageLabel(token.slug), href: `/docs/${token.slug}` },
                  children: [],
                }
              : {
                  type: 'element',
                  tagName: 'SourceRef',
                  properties: { id: token.ref },
                  children: [],
                },
          );
        });
        lastIndex = start + match[0].length;
      }

      if (lastIndex < node.value.length) {
        nodes.push({ type: 'text', value: node.value.slice(lastIndex) });
      }

      parent.children.splice(index, 1, ...nodes);
      return index + nodes.length;
    });
  };
}

// Label a page citation pill with the last slug segment (e.g.
// `concepts/Self-Inductance` → `Self-Inductance`), keeping the pill compact
// while the href carries the full slug.
function pageLabel(slug: string): string {
  const segment = slug.split('/').filter(Boolean).at(-1) ?? slug;
  // The slug segment may be percent-encoded (fumadocs encodes non-ASCII); decode
  // so a Chinese page cites as `感应` rather than `%E6%84…`.
  return safeDecodeURIComponent(segment).replace(/-/g, ' ');
}

function shouldSkipWrap(node: Extract<RootContent, { type: 'element' }>): boolean {
  if (node.tagName === 'pre' || node.tagName === 'code') return true;
  const className = node.properties?.className;
  const classes = Array.isArray(className) ? className : [className];
  return classes.some((value) => String(value).includes('katex'));
}

function mathClasses(node: Element): string[] {
  const value = node.properties?.className;
  if (Array.isArray(value)) return value.map((entry) => String(entry));
  if (typeof value === 'string') return value.split(/\s+/);
  return [];
}

function rawTex(node: ElementContent): string {
  if (node.type === 'text') return node.value;
  if (node.type === 'element') return node.children.map(rawTex).join('');
  return '';
}

function isMathCode(node: ElementContent): boolean {
  return (
    node.type === 'element' &&
    node.tagName === 'code' &&
    mathClasses(node).includes('language-math')
  );
}

function katexMathNode(tex: string, display: boolean): Element {
  return {
    type: 'element',
    tagName: 'KatexMath',
    properties: { tex, display: display ? 'block' : 'inline' },
    children: [],
  };
}

// Replace remark-math output (`<code class="language-math …">tex</code>` and the
// `<pre>`-wrapped display variant) with a single `<KatexMath>` element carrying
// the raw TeX. KaTeX runs in the component via `dangerouslySetInnerHTML`, so the
// rendered tree keeps one element per formula instead of the hundreds of spans
// `rehype-katex` would emit — lighter to reconcile on every streamed chunk.
// Component lookup is safe here because this runs through `toJsxRuntime` with an
// explicit components map (no MDX provider / bare-reference concerns).
export function rehypeChatMath() {
  return (tree: Root) => {
    const walk = (node: { children: ElementContent[] }): void => {
      const out: ElementContent[] = [];
      for (const child of node.children) {
        if (
          child.type === 'element' &&
          child.tagName === 'pre' &&
          child.children.length === 1 &&
          isMathCode(child.children[0])
        ) {
          out.push(katexMathNode(rawTex(child.children[0]), true));
          continue;
        }
        if (child.type === 'element' && isMathCode(child)) {
          out.push(katexMathNode(rawTex(child), mathClasses(child).includes('math-display')));
          continue;
        }
        if (child.type === 'element') walk(child);
        out.push(child);
      }
      node.children = out;
    };

    walk(tree as unknown as { children: ElementContent[] });
  };
}

function KatexMath({ tex, display }: { tex?: string; display?: string }) {
  if (!tex) return null;

  const isBlock = display === 'block';
  const className = isBlock ? 'math math-display' : 'math math-inline';
  const html = renderKatexToString(tex, isBlock);

  return isBlock ? (
    <div className={className} dangerouslySetInnerHTML={{ __html: html }} />
  ) : (
    <span className={className} dangerouslySetInnerHTML={{ __html: html }} />
  );
}

function createProcessor(): Processor {
  const processor = remark()
    .use(remarkGfm)
    .use(remarkMath)
    .use(remarkRehype)
    .use(rehypeChatMath)
    .use(rehypeSourceRefs)
    .use(rehypeWrapWords);

  return {
    async process(content, options) {
      const nodes = processor.parse({ value: content });
      const hast = await processor.run(nodes);

      return toJsxRuntime(hast, {
        development: false,
        jsx,
        jsxs,
        Fragment,
        components: {
          ...defaultMdxComponents,
          pre: Pre,
          ...(options?.inline ? { p: InlineParagraph } : {}),
          img: IgnoredImage,
          SourceRef,
          KatexMath,
        },
      });
    },
  };
}

function Pre(props: ComponentProps<'pre'>) {
  const code = Children.only(props.children) as ReactElement;
  const codeProps = code.props as ComponentProps<'code'>;
  const content = codeProps.children;
  if (typeof content !== 'string') return null;

  let lang =
    codeProps.className
      ?.split(' ')
      .find((value) => value.startsWith('language-'))
      ?.slice('language-'.length) ?? 'text';

  if (lang === 'mdx') lang = 'md';

  return <DynamicCodeBlock lang={lang} code={content.trimEnd()} />;
}

function InlineParagraph(props: ComponentProps<'span'>) {
  return <span {...props} />;
}

function IgnoredImage() {
  return null;
}

const processor = createProcessor();
const cache = new Map<string, Promise<ReactNode>>();

export function Markdown({ text, inline = false }: { text: string; inline?: boolean }) {
  const deferredText = useDeferredValue(text);

  return (
    <Suspense fallback={<span className="invisible">{text}</span>}>
      <Renderer inline={inline} text={deferredText} />
    </Suspense>
  );
}

function Renderer({ text, inline }: { text: string; inline: boolean }) {
  const key = `${inline ? 'inline' : 'block'}:${text}`;
  const result = cache.get(key) ?? processor.process(text, { inline });
  cache.set(key, result);

  return use(result);
}
