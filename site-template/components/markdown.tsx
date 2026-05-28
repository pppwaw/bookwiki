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
import { SourceRef } from './SourceRef';

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

const sourceRefCitationPattern =
  /\[\^([A-Za-z0-9_.:-]+)\](?!:)|\[([A-Za-z0-9_.:-]+-p\d+[A-Za-z0-9_.:-]*)\](?!\()/g;

export function rehypeSourceRefs() {
  return (tree: Root) => {
    visit(tree, 'text', (node, index, parent) => {
      if (!parent || index === undefined || !sourceRefCitationPattern.test(node.value)) {
        sourceRefCitationPattern.lastIndex = 0;
        return;
      }

      sourceRefCitationPattern.lastIndex = 0;
      const nodes: ElementContent[] = [];
      let lastIndex = 0;

      for (const match of node.value.matchAll(sourceRefCitationPattern)) {
        const start = match.index ?? 0;
        const refId = match[1] ?? match[2];
        if (!refId) continue;

        if (start > lastIndex) {
          nodes.push({ type: 'text', value: node.value.slice(lastIndex, start) });
        }

        nodes.push({
          type: 'element',
          tagName: 'SourceRef',
          properties: { id: refId },
          children: [],
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
