import defaultMdxComponents from 'fumadocs-ui/mdx';
import type { MDXComponents } from 'mdx/types';
import { AnkiDeck } from './AnkiDeck';
import { ChatBox } from './ChatBox';
import { ConceptLink } from './ConceptLink';
import { QuizBlock } from './QuizBlock';
import { SourceRef } from './SourceRef';

export function getMDXComponents(components?: MDXComponents) {
  return {
    ...defaultMdxComponents,
    ConceptLink,
    QuizBlock,
    AnkiDeck,
    SourceRef,
    ChatBox,
    ...components,
  } satisfies MDXComponents;
}

export const useMDXComponents = getMDXComponents;

declare global {
  type MDXProvidedComponents = ReturnType<typeof getMDXComponents>;
}
