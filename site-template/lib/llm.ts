import type { SearchChunk } from './rag';
import { contextFromChunks } from './rag';

export async function answerWithChatModel({
  question,
  chunks,
  model,
}: {
  question: string;
  chunks: SearchChunk[];
  model: string;
}) {
  const apiKey = process.env.BOOKWIKI_CHAT_API_KEY ?? process.env.GEMMA_API_KEY;
  const baseUrl = process.env.BOOKWIKI_CHAT_BASE_URL ?? 'https://api.openai.com/v1';

  if (!apiKey) {
    throw new Error('BOOKWIKI_CHAT_API_KEY is required for /api/chat');
  }

  const response = await fetch(`${baseUrl.replace(/\/$/, '')}/chat/completions`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${apiKey}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({
      model,
      messages: [
        {
          role: 'system',
          content:
            'Answer only from the provided BookWiki chunks. If the chunks do not contain the answer, say that the book content did not provide enough evidence. Include source_ref IDs in the answer.',
        },
        {
          role: 'user',
          content: `Question: ${question}\n\n${contextFromChunks(chunks)}`,
        },
      ],
      temperature: 0.2,
    }),
  });

  if (!response.ok) {
    const body = await response.text();
    throw new Error(`chat model request failed: ${response.status} ${body}`);
  }

  const payload = (await response.json()) as {
    choices?: Array<{ message?: { content?: string } }>;
  };

  return payload.choices?.[0]?.message?.content?.trim() ?? '';
}
