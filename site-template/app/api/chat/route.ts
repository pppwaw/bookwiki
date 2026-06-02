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

type ChatResponse = {
  answer: string;
  sources: ChatSource[];
};

type ChatCompletionResponse = {
  choices?: Array<{
    message?: {
      content?: string | null;
    };
  }>;
  error?: {
    message?: string;
  };
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

    const baseUrl = (process.env.BOOKWIKI_CHAT_BASE_URL ?? 'http://127.0.0.1:1234/v1').replace(/\/+$/, '');
    const model = process.env.BOOKWIKI_CHAT_MODEL ?? 'local-model';
    const currentArticle = await currentArticleFromPath(pagePath);
    const chunks = searchChunks(question, 6, chapterId);
    const sources = collectSources(currentArticle, chunks);
    const answer = await answerWithChatModel({ apiKey, baseUrl, model, question, currentArticle, chunks });

    return Response.json({ answer, sources } satisfies ChatResponse);
  } catch (error) {
    return Response.json(
      {
        error: error instanceof Error ? error.message : 'chat request failed',
      },
      { status: 503 },
    );
  }
}

async function answerWithChatModel({
  apiKey,
  baseUrl,
  model,
  question,
  currentArticle,
  chunks,
}: {
  apiKey: string;
  baseUrl: string;
  model: string;
  question: string;
  currentArticle: CurrentArticle | null;
  chunks: SearchChunk[];
}) {
  const response = await fetch(`${baseUrl}/chat/completions`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${apiKey}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      model,
      temperature: 0.2,
      messages: [
        {
          role: 'system',
          content: [
            'You answer questions about a single BookWiki vault.',
            'Use only the provided current article and search snippets as evidence.',
            'If the evidence is insufficient, say so directly.',
            'Answer in concise Markdown.',
            'Cite evidence with source_ref footnote markers such as [^Week-10-p008] when source refs are available.',
            'Do not invent source_ref IDs.',
          ].join(' '),
        },
        {
          role: 'user',
          content: promptFromQuestion(question, currentArticle, chunks),
        },
      ],
    }),
  });

  const payload = (await response.json().catch(() => ({}))) as ChatCompletionResponse;
  if (!response.ok) {
    throw new Error(payload.error?.message ?? `chat model request failed: HTTP ${response.status}`);
  }

  const answer = payload.choices?.[0]?.message?.content?.trim();
  if (!answer) {
    throw new Error('chat model returned an empty answer');
  }
  return answer;
}

function promptFromQuestion(question: string, article: CurrentArticle | null, chunks: SearchChunk[]) {
  const sections = [`Question: ${question}`];

  if (article) {
    sections.push(
      [
        `Current article: ${article.title} (${article.slug})`,
        `Source refs on current article: ${article.sourceRefs.join(', ') || 'none'}`,
        '<current_article>',
        article.text,
        '</current_article>',
      ].join('\n'),
    );
  }

  if (chunks.length) {
    sections.push(
      [
        '<search_results>',
        ...chunks.map((chunk, index) =>
          [
            `Result ${index + 1}: ${chunk.slug}`,
            chunk.headingPath ? `Heading: ${chunk.headingPath}` : null,
            `Source refs: ${chunk.sourceRefs.join(', ') || 'none'}`,
            chunk.text,
          ]
            .filter(Boolean)
            .join('\n'),
        ),
        '</search_results>',
      ].join('\n\n'),
    );
  }

  return sections.join('\n\n');
}

function questionFromBody(body: ChatRequest) {
  if (typeof body.question === 'string') return body.question.trim();
  return textFromMessage(body.message).trim();
}

function textFromMessage(value: unknown) {
  if (typeof value === 'string') return value;
  if (!isUiMessage(value)) return '';
  return value.parts
    .filter((part): part is { type: 'text'; text: string } => part.type === 'text' && typeof part.text === 'string')
    .map((part) => part.text)
    .join('\n');
}

function isUiMessage(value: unknown): value is { parts: Array<{ type: string; text?: unknown }> } {
  return (
    typeof value === 'object' &&
    value !== null &&
    'parts' in value &&
    Array.isArray((value as { parts?: unknown }).parts)
  );
}

function collectSources(article: CurrentArticle | null, chunks: SearchChunk[]) {
  const sources = new Map<string, ChatSource>();
  if (article) addArticleSources(sources, article);
  addChunkSources(sources, chunks);
  return [...sources.values()];
}

function addChunkSources(sources: Map<string, ChatSource>, chunks: SearchChunk[]) {
  for (const chunk of chunks) {
    for (const refId of chunk.sourceRefs) {
      if (!sources.has(refId)) {
        sources.set(refId, {
          ref_id: refId,
          page: chunk.slug,
          heading: chunk.headingPath,
        });
      }
    }
  }
}

function addArticleSources(sources: Map<string, ChatSource>, article: CurrentArticle) {
  for (const refId of article.sourceRefs) {
    if (!sources.has(refId)) {
      sources.set(refId, {
        ref_id: refId,
        page: article.slug,
        heading: article.title,
      });
    }
  }
}
