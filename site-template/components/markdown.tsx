'use client';

import { remark } from 'remark';
import remarkGfm from 'remark-gfm';
import remarkMath from 'remark-math';
import remarkRehype from 'remark-rehype';
import rehypeKatex from 'rehype-katex';
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
import type { ElementContent, Root, RootContent } from 'hast';

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

function shouldSkipWrap(node: Extract<RootContent, { type: 'element' }>): boolean {
  if (node.tagName === 'pre' || node.tagName === 'code') return true;
  const className = node.properties?.className;
  const classes = Array.isArray(className) ? className : [className];
  return classes.some((value) => String(value).includes('katex'));
}

function createProcessor(): Processor {
  const processor = remark()
    .use(remarkGfm)
    .use(remarkMath)
    .use(remarkRehype)
    .use(rehypeKatex)
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
          p: options?.inline ? InlineParagraph : undefined,
          img: undefined,
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
