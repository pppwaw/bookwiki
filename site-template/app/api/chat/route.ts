import { answerWithChatModel } from '@/lib/llm';
import { searchChunks } from '@/lib/rag';

export const runtime = 'nodejs';

export async function POST(request: Request) {
  const body = (await request.json()) as {
    question?: unknown;
    chapterId?: unknown;
  };
  const question = typeof body.question === 'string' ? body.question.trim() : '';
  const chapterId = typeof body.chapterId === 'string' ? body.chapterId : undefined;

  if (!question) {
    return Response.json({ error: 'question is required' }, { status: 400 });
  }

  const chunks = searchChunks(question, 6, chapterId);
  const sources = chunks.flatMap((chunk) =>
    chunk.sourceRefs.map((refId) => ({
      ref_id: refId,
      page: chunk.slug,
      heading: chunk.headingPath,
    })),
  );

  if (chunks.length === 0) {
    return Response.json({
      answer: 'No matching BookWiki chunks were found for this question.',
      sources,
    });
  }

  try {
    const model = process.env.BOOKWIKI_CHAT_MODEL ?? 'gemma-4';
    const answer = await answerWithChatModel({ question, chunks, model });
    return Response.json({ answer, sources });
  } catch (error) {
    return Response.json(
      {
        error: error instanceof Error ? error.message : 'chat request failed',
        sources,
      },
      { status: 503 },
    );
  }
}
