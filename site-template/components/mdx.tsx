import defaultMdxComponents from 'fumadocs-ui/mdx';
import type { MDXComponents } from 'mdx/types';
import { AnkiBack, AnkiCard, AnkiDeck, AnkiFront } from './AnkiDeck';
import { ChapterSummary } from './ChapterSummary';
import { ChatBox } from './ChatBox';
import { BookFigure } from './BookFigure';
import { ConceptLink } from './ConceptLink';
import { Mermaid } from './Mermaid';
import { PreviewLink } from './PreviewLink';
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
import { WorkedProblem } from './WorkedProblem';

export function getMDXComponents(components?: MDXComponents) {
  return {
    ...defaultMdxComponents,
    ChapterSummary,
    ConceptLink,
    PreviewLink,
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
    BookFigure,
    Mermaid,
    WorkedProblem,
    ...components,
  } satisfies MDXComponents;
}

export const useMDXComponents = getMDXComponents;

declare global {
  type MDXProvidedComponents = ReturnType<typeof getMDXComponents>;
}
