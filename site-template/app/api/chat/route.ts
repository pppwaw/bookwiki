import { createOpenRouter } from '@openrouter/ai-sdk-provider';
import { stepCountIs, streamText, tool, type UIMessage } from 'ai';
import { z } from 'zod';
import { currentArticleFromPath, searchChunks, type CurrentArticle, type SearchChunk } from '@/lib/rag';

export const runtime = 'nodejs';

type ChatSource = {
  ref_id: string;
  page?: string;
  heading?: string | null;
};

type ChatRequest = {
  message?: unknown;
  question?: unknown;
  chapterId?: unknown;
  pagePath?: unknown;
};

export async function POST(request: Request) {
  let body: ChatRequest;

  try {
    body = (await request.json()) as ChatRequest;
  } catch {
    return Response.json({ error: 'valid JSON body is required' }, { status: 400 });
  }

  try {
    const question = questionFromBody(body);
    const chapterId = typeof body.chapterId === 'string' ? body.chapterId : undefined;
    const pagePath = typeof body.pagePath === 'string' ? body.pagePath : undefined;

    if (!question) {
      return Response.json({ error: 'question is required' }, { status: 400 });
    }

    const apiKey = process.env.BOOKWIKI_CHAT_API_KEY;
    if (!apiKey) {
      return Response.json(
        { error: 'BOOKWIKI_CHAT_API_KEY is required for /api/chat' },
        { status: 503 },
      );
    }

    const sources = new Map<string, ChatSource>();
    const openrouter = createOpenRouter({
      apiKey,
      baseURL: process.env.BOOKWIKI_CHAT_BASE_URL ?? 'https://openrouter.ai/api/v1',
      appName: 'BookWiki',
    });
    const currentArticle = await currentArticleFromPath(pagePath);
    if (currentArticle) addArticleSources(sources, currentArticle);
    let answerText = '';

    const result = streamText({
      model: openrouter(process.env.BOOKWIKI_CHAT_MODEL ?? 'google/gemma-4-31b-it'),
      system: [
        'You answer questions about a single BookWiki vault.',
        'The current article is provided in the user message when available.',
        'Use search_book when the current article does not contain enough evidence.',
        'Answer only from the current article context and tool results. If evidence is insufficient, say so.',
        chatFormatInstructions(),
      ].join(' '),
      prompt: promptFromQuestion(question, currentArticle),
      providerOptions: {
        openrouter: {
          reasoning: {
            enabled: true,
            exclude: false,
            effort: 'low',
          },
        },
      },
      stopWhen: stepCountIs(4),
      tools: {
        get_current_article: tool({
          description: 'Return the full current documentation article as markdown for grounding.',
          inputSchema: z.object({}),
          execute: async () => {
            if (!currentArticle) {
              return {
                found: false,
                message: 'No current article was provided or matched.',
              };
            }
            return {
              found: true,
              slug: currentArticle.slug,
              title: currentArticle.title,
              sourceRefs: currentArticle.sourceRefs,
              text: currentArticle.text,
            };
          },
        }),
        search_book: tool({
          description:
            'Search the BookWiki SQLite index. Use this for questions requiring evidence outside the current article.',
          inputSchema: z.object({
            query: z.string().min(1).describe('Search query for BookWiki content.'),
            limit: z.number().int().min(1).max(8).default(6),
            chapterId: z.string().optional().describe('Optional chapter id filter.'),
          }),
          execute: async ({ query, limit, chapterId: requestedChapterId }) => {
            const chunks = searchChunks(query, limit, requestedChapterId ?? chapterId);
            addChunkSources(sources, chunks);
            return {
              query,
              chunks: chunks.map((chunk) => ({
                chunkId: chunk.chunkId,
                page: chunk.slug,
                heading: chunk.headingPath,
                sourceRefs: chunk.sourceRefs,
                text: chunk.text,
              })),
            };
          },
        }),
      },
    });

    return result.toUIMessageStreamResponse({
      messageMetadata: ({ part }) => {
        if (part.type === 'text-delta') answerText += part.text;
        if (part.type !== 'finish') return undefined;
        return { sources: citedSourcesFromText(answerText, sources) };
      },
      onError: (error) => (error instanceof Error ? error.message : 'chat request failed'),
    });
  } catch (error) {
    return Response.json(
      {
        error: error instanceof Error ? error.message : 'chat request failed',
      },
      { status: 503 },
    );
  }
}

function chatFormatInstructions() {
  return [
    'Format answers as concise GitHub-flavored Markdown.',
    'Cite evidence with source_ref footnote markers, for example [^Week-10-p008].',
    'Only cite source_ref IDs that appear in the current article context or tool results.',
    'Do not invent source_ref IDs.',
    'Do not add footnote definition blocks; the BookWiki UI renders source_ref markers directly.',
  ].join(' ');
}

function questionFromBody(body: ChatRequest) {
  if (typeof body.question === 'string') return body.question.trim();
  return textFromMessage(body.message).trim();
}

function textFromMessage(value: unknown) {
  if (!isUiMessage(value)) return '';
  return value.parts
    .filter((part): part is Extract<UIMessage['parts'][number], { type: 'text' }> => part.type === 'text')
    .map((part) => part.text)
    .join('\n');
}

function isUiMessage(value: unknown): value is UIMessage {
  return (
    typeof value === 'object' &&
    value !== null &&
    'parts' in value &&
    Array.isArray((value as { parts?: unknown }).parts)
  );
}

function promptFromQuestion(question: string, article: CurrentArticle | null) {
  if (!article) {
    return `Question: ${question}`;
  }

  return [
    `Current article: ${article.title} (${article.slug})`,
    `Source refs on current article: ${article.sourceRefs.join(', ') || 'none'}`,
    '<current_article>',
    article.text,
    '</current_article>',
    `Question: ${question}`,
  ].join('\n\n');
}

function addChunkSources(sources: Map<string, ChatSource>, chunks: SearchChunk[]) {
  for (const chunk of chunks) {
    for (const refId of chunk.sourceRefs) {
      sources.set(`${refId}:${chunk.slug}:${chunk.headingPath ?? ''}`, {
        ref_id: refId,
        page: chunk.slug,
        heading: chunk.headingPath,
      });
    }
  }
}

function addArticleSources(sources: Map<string, ChatSource>, article: CurrentArticle) {
  for (const refId of article.sourceRefs) {
    sources.set(`${refId}:${article.slug}`, {
      ref_id: refId,
      page: article.slug,
      heading: article.title,
    });
  }
}

function citedSourcesFromText(text: string, sources: Map<string, ChatSource>) {
  const citedRefs = citedSourceRefs(text);
  const seenRefs = new Set<string>();
  const citedSources: ChatSource[] = [];

  for (const source of sources.values()) {
    if (!citedRefs.has(source.ref_id) || seenRefs.has(source.ref_id)) continue;
    citedSources.push(source);
    seenRefs.add(source.ref_id);
  }

  return citedSources;
}

function citedSourceRefs(text: string) {
  const refs = new Set<string>();
  const pattern = /\[\^([A-Za-z0-9_.:-]+)\](?!:)|\[([A-Za-z0-9_.:-]+-p\d+[A-Za-z0-9_.:-]*)\](?!\()/g;

  for (const match of text.matchAll(pattern)) {
    const refId = match[1] ?? match[2];
    if (refId) refs.add(refId);
  }

  return refs;
}
