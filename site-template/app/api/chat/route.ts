import { createOpenRouter } from '@openrouter/ai-sdk-provider';
import { convertToModelMessages, stepCountIs, streamText, tool, type UIMessage } from 'ai';
import { z } from 'zod';
import {
  currentArticleFromPath,
  pageBySlug,
  searchChunks,
  type CurrentArticle,
  type SearchChunk,
} from '@/lib/rag';
import { citationGroupRegex, tokensFromMatch } from '@/lib/citations';
import {
  articleTokenBudget,
  modelContextTokens,
  positiveIntFromEnv,
  truncateToTokenBudget,
} from '@/lib/model-context';

export const runtime = 'nodejs';

const DefaultModel = 'google/gemma-4-31b-it';
const DefaultBaseURL = 'https://openrouter.ai/api/v1';
const DefaultContextTokens = 32000;
const OutputTokens = 2048;

type ChatSource = {
  ref_id: string;
  page?: string;
  heading?: string | null;
  /** Set for page citations: an in-app link to the cited BookWiki page. */
  url?: string;
};

type ChatRequest = {
  messages?: unknown;
  message?: unknown;
  question?: unknown;
  chapterId?: unknown;
  pagePath?: unknown;
  /**
   * Optional custom system prompt. When provided (non-empty string), it fully
   * replaces the default "BookWiki vault assistant" persona while preserving
   * the grounded current article and tool/format rules. Used by the Feynman
   * learning mode to switch between the confused-peer probe persona and the
   * reviewer persona across multi-turn dialogue.
   */
  system?: unknown;
};

export async function POST(request: Request) {
  let body: ChatRequest;

  try {
    body = (await request.json()) as ChatRequest;
  } catch {
    return Response.json({ error: 'valid JSON body is required' }, { status: 400 });
  }

  try {
    const chapterId = typeof body.chapterId === 'string' ? body.chapterId : undefined;
    const pagePath = typeof body.pagePath === 'string' ? body.pagePath : undefined;
    const customSystem = typeof body.system === 'string' ? body.system.trim() : '';
    const uiMessages = messagesFromBody(body);

    if (uiMessages.length === 0) {
      return Response.json({ error: 'a message is required' }, { status: 400 });
    }

    const apiKey = process.env.BOOKWIKI_CHAT_API_KEY;
    if (!apiKey) {
      return Response.json(
        { error: 'BOOKWIKI_CHAT_API_KEY is required for /api/chat' },
        { status: 503 },
      );
    }

    const model = process.env.BOOKWIKI_CHAT_MODEL ?? DefaultModel;
    const baseURL = process.env.BOOKWIKI_CHAT_BASE_URL ?? DefaultBaseURL;

    const sources = new Map<string, ChatSource>();
    const openrouter = createOpenRouter({ apiKey, baseURL, appName: 'BookWiki' });

    // Trim the grounding article to the model's context window so large pages do
    // not overflow it. Falls back to an env-configured window when unknown.
    const fallbackContext = positiveIntFromEnv('BOOKWIKI_CHAT_CONTEXT_TOKENS') ?? DefaultContextTokens;
    const contextTokens = (await modelContextTokens(model, apiKey, baseURL)) ?? fallbackContext;
    const tokenBudget = articleTokenBudget(contextTokens, OutputTokens);

    const currentArticle = await currentArticleFromPath(pagePath);
    const groundingText = currentArticle ? budgetArticleText(currentArticle.text, tokenBudget) : '';
    if (currentArticle) addArticleSources(sources, currentArticle);

    let answerText = '';

    const result = streamText({
      model: openrouter(model),
      system: systemPrompt(currentArticle, groundingText, customSystem),
      messages: await convertHistory(uiMessages),
      maxOutputTokens: OutputTokens,
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
          description: 'Return the current documentation article as markdown for grounding.',
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
              text: groundingText,
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
            const chunks = await searchChunks(query, limit, requestedChapterId ?? chapterId);
            addChunkSources(sources, chunks);
            return {
              query,
              chunks: chunks.map((chunk) => ({
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
    'Cite evidence with footnote markers like [^Week-10-p008].',
    'Use the source_ref id when the evidence has one; otherwise cite the page by its slug exactly as it appears in the tool result "page" field or the current article slug, e.g. [^concepts/Self-Inductance].',
    'Only cite source_ref ids or page slugs that appear in the current article context or tool results. Do not invent them.',
    'Do not add footnote definition blocks; the BookWiki UI renders these markers directly.',
  ].join(' ');
}

function systemPrompt(article: CurrentArticle | null, groundingText: string, customPersona = '') {
  const intro = customPersona
    ? [
        customPersona,
        'You are operating inside a BookWiki vault.',
        'Stay grounded only in the current article context and tool results; if evidence is insufficient, say so.',
        'Earlier turns of this conversation are included; stay consistent with them.',
        chatFormatInstructions(),
      ].join('\n')
    : [
        'You answer questions about a single BookWiki vault.',
        'The current article is provided below when available.',
        'Use search_book when the current article does not contain enough evidence.',
        'Answer only from the current article context and tool results. If evidence is insufficient, say so.',
        'Earlier turns of this conversation are included; stay consistent with them.',
        chatFormatInstructions(),
      ].join('\n');

  const parts = [intro];

  if (article) {
    parts.push(
      [
        `Current article: ${article.title} (${article.slug})`,
        `Source refs on current article: ${article.sourceRefs.join(', ') || 'none'}`,
        '<current_article>',
        groundingText,
        '</current_article>',
      ].join('\n'),
    );
  }

  return parts.join('\n\n');
}

function budgetArticleText(text: string, tokenBudget: number) {
  let trimmed = truncateToTokenBudget(text, tokenBudget);

  const hardCap = positiveIntFromEnv('BOOKWIKI_CHAT_MAX_ARTICLE_CHARS');
  if (hardCap && trimmed.length > hardCap) trimmed = trimmed.slice(0, hardCap);

  return trimmed.length < text.length ? `${trimmed}\n\n[truncated to fit model context]` : trimmed;
}

function messagesFromBody(body: ChatRequest): UIMessage[] {
  if (Array.isArray(body.messages)) {
    return body.messages.filter(isUiMessage);
  }
  if (isUiMessage(body.message)) {
    return [body.message];
  }
  if (typeof body.question === 'string' && body.question.trim()) {
    return [
      {
        id: 'question',
        role: 'user',
        parts: [{ type: 'text', text: body.question.trim() }],
      } as UIMessage,
    ];
  }
  return [];
}

async function convertHistory(messages: UIMessage[]) {
  try {
    return await convertToModelMessages(messages);
  } catch {
    const textOnly = messages
      .map((message) => ({
        ...message,
        parts: message.parts.filter((part) => part.type === 'text'),
      }))
      .filter((message) => message.parts.length > 0);
    return await convertToModelMessages(textOnly);
  }
}

function isUiMessage(value: unknown): value is UIMessage {
  return (
    typeof value === 'object' &&
    value !== null &&
    'parts' in value &&
    Array.isArray((value as { parts?: unknown }).parts)
  );
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
  const { sourceRefs, pageSlugs } = parseCitedRefs(text);
  const seenRefs = new Set<string>();
  const citedSources: ChatSource[] = [];

  for (const source of sources.values()) {
    if (!sourceRefs.has(source.ref_id) || seenRefs.has(source.ref_id)) continue;
    citedSources.push(source);
    seenRefs.add(source.ref_id);
  }

  // Resolve page citations into entries that link back to the cited page. Slugs
  // are looked up so titles can be shown and unknown slugs are dropped instead
  // of producing dead links.
  for (const slug of pageSlugs) {
    const page = pageBySlug(slug);
    if (!page) continue;
    citedSources.push({
      ref_id: `page:${slug}`,
      page: slug,
      heading: page.title,
      url: `/docs/${slug}`,
    });
  }

  return citedSources;
}

function parseCitedRefs(text: string) {
  const sourceRefs = new Set<string>();
  const pageSlugs = new Set<string>();

  for (const match of text.matchAll(citationGroupRegex())) {
    const tokens = tokensFromMatch(match);
    if (!tokens) continue;
    for (const token of tokens) {
      if (token.kind === 'page') pageSlugs.add(token.slug);
      else sourceRefs.add(token.ref);
    }
  }

  return { sourceRefs, pageSlugs };
}
