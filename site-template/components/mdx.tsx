import defaultMdxComponents from "fumadocs-ui/mdx";
import { AnkiDeck } from "./AnkiDeck";
import { QuizBlock } from "./QuizBlock";
import { SourceRef } from "./SourceRef";

type Components = Record<string, unknown>;

export function getMDXComponents(components?: Components) {
  return {
    ...defaultMdxComponents,
    QuizBlock,
    AnkiDeck,
    SourceRef,
    ...components,
  };
}

export const useMDXComponents = getMDXComponents;
