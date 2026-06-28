import { createOpenRouter } from '@openrouter/ai-sdk-provider';
import { generateText, Output } from 'ai';
import { z } from 'zod';
import { searchChunks } from '@/lib/rag';

export const runtime = 'nodejs';

const DefaultModel = 'google/gemma-4-31b-it';
const DefaultBaseURL = 'https://openrouter.ai/api/v1';
const MaxGroundingChunks = 4;
const OutputTokens = 1800;
const MaxQuestionChars = 8000;
const MaxReferenceAnswerChars = 16000;
const MaxRubricPoints = 24;
const MaxRubricPointChars = 1200;
const MaxUserAnswerChars = 12000;

const rubricPointSchema = z.object({
  point: z.string().min(1).max(MaxRubricPointChars),
  weight: z.number().positive(),
});

const evaluateRequestSchema = z.object({
  question: z.string().min(1).max(MaxQuestionChars),
  reference_answer: z.string().min(1).max(MaxReferenceAnswerChars),
  rubric: z.array(rubricPointSchema).min(1).max(MaxRubricPoints),
  user_answer: z.string().min(1).max(MaxUserAnswerChars),
  chapter_id: z.string().optional(),
});

const evaluateOutputSchema = z.object({
  point_scores: z.array(
    z.object({
      point: z.string().min(1).max(MaxRubricPointChars),
      earned: z.number().min(0),
    }),
  ),
  feedback: z.string().min(1),
  revised_answer: z.string().min(1),
});

type EvaluateRequest = z.infer<typeof evaluateRequestSchema>;

export async function POST(request: Request) {
  let payload: EvaluateRequest;

  try {
    payload = evaluateRequestSchema.parse(await request.json());
  } catch {
    return Response.json({ error: 'valid evaluation JSON body is required' }, { status: 400 });
  }

  const apiKey = process.env.BOOKWIKI_EVALUATE_API_KEY ?? process.env.BOOKWIKI_CHAT_API_KEY;
  if (!apiKey) {
    return Response.json(
      { error: 'BOOKWIKI_EVALUATE_API_KEY or BOOKWIKI_CHAT_API_KEY is required for /api/evaluate' },
      { status: 503 },
    );
  }

  const model = process.env.BOOKWIKI_EVALUATE_MODEL ?? process.env.BOOKWIKI_CHAT_MODEL ?? DefaultModel;
  const baseURL = process.env.BOOKWIKI_EVALUATE_BASE_URL ?? process.env.BOOKWIKI_CHAT_BASE_URL ?? DefaultBaseURL;
  const openrouter = createOpenRouter({ apiKey, baseURL, appName: 'BookWiki' });
  const maxScore = payload.rubric.reduce((total, point) => total + point.weight, 0);
  const grounding = groundingText(payload);

  // Stream NDJSON to the browser: a once-per-second heartbeat keeps the
  // connection alive while grading runs (the model output is structured JSON, so
  // there is nothing useful to render incrementally — the client just shows a
  // "judging" state), then a single terminal line carries the server-scored
  // verdict. Scoring stays authoritative on the server — the model proposes a
  // per-point earned score, the server clamps each to [0, weight] and sums them.
  // Upstream we use generateText (not streamText): its one-shot
  // structured-output parse is robust, whereas streaming object parsing breaks
  // when reasoning tokens interleave ("No object generated").
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      const send = (event: unknown) => controller.enqueue(encoder.encode(`${JSON.stringify(event)}\n`));
      const heartbeat = setInterval(() => {
        try {
          send({ type: 'progress' });
        } catch {
          // client disconnected — let the awaited generation settle and clean up
        }
      }, 1000);
      try {
        const result = await generateText({
          model: openrouter(model),
          maxOutputTokens: OutputTokens,
          output: Output.object({ schema: evaluateOutputSchema }),
          system: systemPrompt(grounding),
          prompt: userPrompt(payload, maxScore),
          providerOptions: {
            openrouter: {
              reasoning: {
                enabled: true,
                exclude: false,
                effort: 'medium',
              },
            },
          },
        });

        const output = result.output;
        const points = scoredPoints(output.point_scores, payload.rubric);
        const score = points.reduce((total, point) => total + point.earned, 0);
        send({
          type: 'result',
          result: {
            verdict: verdictFor(score, maxScore),
            score,
            max_score: maxScore,
            points,
            feedback: output.feedback,
            revised_answer: output.revised_answer,
          },
        });
      } catch (error) {
        send({ type: 'error', error: error instanceof Error ? error.message : 'evaluation request failed' });
      } finally {
        clearInterval(heartbeat);
        controller.close();
      }
    },
  });

  return new Response(stream, {
    headers: {
      'Content-Type': 'application/x-ndjson; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      'X-Accel-Buffering': 'no',
    },
  });
}

function systemPrompt(grounding: string) {
  return [
    '你是 BookWiki 的证明题/计算题判分器。',
    '只做单次判分,不要开启多轮对话。',
    '严格对照参考答案和 rubric 逐条给分:每个 rubric.point 单独打分。',
    '完全命中给满该点 weight;部分命中给 0 到 weight 之间的分;未命中或明显错误给 0。',
    '在 revised_answer 中基于用户答案修补成一份完整过程,不要只复制参考答案。',
    '所有公式保留为 LaTeX 的 $...$ 或 $$...$$。',
    grounding ? `\n<book_grounding>\n${grounding}\n</book_grounding>` : '',
  ].join('\n');
}

function userPrompt(payload: EvaluateRequest, maxScore: number) {
  return JSON.stringify(
    {
      task: 'grade_worked_answer',
      max_score: maxScore,
      question: payload.question,
      reference_answer: payload.reference_answer,
      rubric: payload.rubric,
      user_answer: payload.user_answer,
      scoring_rules: [
        'point_scores 必须为每个 rubric.point 各给一项,point 字段逐字复制 rubric.point,不要改写或概括。',
        'earned 是该点拿到的分,范围 0 到该点 weight;部分正确给中间分。',
        '服务端会把 earned 夹到 [0, weight] 并求和得到总分,不要自行给总分。',
        'feedback 用一段中文指出主要正确处、错误处和下一步怎么改。',
        'revised_answer 必须是一份修补后的完整解题过程。',
      ],
    },
    null,
    2,
  );
}

function groundingText(payload: EvaluateRequest) {
  const chunks = searchChunks(payload.question, MaxGroundingChunks, payload.chapter_id);
  return chunks
    .map((chunk) => [`[${chunk.chunkId}] ${chunk.title}`, chunk.headingPath ?? '', chunk.text].filter(Boolean).join('\n'))
    .join('\n\n---\n\n');
}

// Map the model's per-point earned scores back onto the canonical rubric (in
// rubric order, one entry per point), clamping each to [0, weight] so the model
// can never inflate a point past its weight or hand out negative credit. Points
// the model forgot default to 0.
function scoredPoints(
  pointScores: Array<{ point: string; earned: number }>,
  rubric: EvaluateRequest['rubric'],
) {
  const earnedByPoint = new Map<string, number>();
  for (const entry of pointScores) {
    if (!earnedByPoint.has(entry.point)) earnedByPoint.set(entry.point, entry.earned);
  }
  return rubric.map((point) => {
    const raw = earnedByPoint.get(point.point) ?? 0;
    const earned = Math.min(Math.max(raw, 0), point.weight);
    return { point: point.point, earned, weight: point.weight };
  });
}

function verdictFor(score: number, maxScore: number) {
  if (score >= maxScore) return 'correct';
  if (score <= 0) return 'incorrect';
  return 'partial';
}
