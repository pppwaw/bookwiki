import defaultMdxComponents from 'fumadocs-ui/mdx';
import type { MDXComponents } from 'mdx/types';
import { AnkiBack, AnkiCard, AnkiDeck, AnkiFront } from './AnkiDeck';
import { ChatBox } from './ChatBox';
import { ConceptLink } from './ConceptLink';
import {
  QuizBlock,
  QuizCheck,
  QuizChoice,
  QuizChoices,
  QuizExplanation,
  QuizItem,
  QuizQuestion,
} from './QuizBlock';
import { SourceRef } from './SourceRef';

export function getMDXComponents(components?: MDXComponents) {
  return {
    ...defaultMdxComponents,
    ConceptLink,
    QuizBlock,
    QuizItem,
    QuizQuestion,
    QuizChoices,
    QuizChoice,
    QuizCheck,
    QuizExplanation,
    AnkiDeck,
    AnkiCard,
    AnkiFront,
    AnkiBack,
    SourceRef,
    ChatBox,
    ...components,
  } satisfies MDXComponents;
}

export const useMDXComponents = getMDXComponents;

declare global {
  type MDXProvidedComponents = ReturnType<typeof getMDXComponents>;
}
