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
  score: z.number().min(0),
  matched_points: z.array(z.string()),
  missing_points: z.array(z.string()),
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

    const matchedPoints = normalizeMatchedPoints(result.output.matched_points, payload.rubric);
    const missingPoints = missingRubricPoints(matchedPoints, payload.rubric);
    const score = scoreFromMatchedPoints(matchedPoints, payload.rubric);
    return Response.json({
      verdict: verdictFor(score, maxScore),
      score,
      max_score: maxScore,
      matched_points: matchedPoints,
      missing_points: missingPoints.length > 0 ? missingPoints : result.output.missing_points,
      feedback: result.output.feedback,
      revised_answer: result.output.revised_answer,
    });
  } catch (error) {
    return Response.json(
      { error: error instanceof Error ? error.message : 'evaluation request failed' },
      { status: 503 },
    );
  }
}

function systemPrompt(grounding: string) {
  return [
    '你是 BookWiki 的证明题/计算题判分器。',
    '只做单次判分,不要开启多轮对话。',
    '严格对照参考答案和 rubric 逐条判断用户解题过程是否命中。',
    '结论正确但过程缺少关键步骤时只能给部分分。',
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
        'matched_points 只能逐字复制命中的 rubric.point,不要改写或概括。',
        'missing_points 只能逐字复制未命中或明显错误的 rubric.point,不要改写或概括。',
        '服务端会按 matched_points 对应的 weight 重算 score。',
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

function normalizeMatchedPoints(points: string[], rubric: EvaluateRequest['rubric']) {
  const allowed = new Set(rubric.map((point) => point.point));
  const matched: string[] = [];
  for (const point of points) {
    if (!allowed.has(point) || matched.includes(point)) continue;
    matched.push(point);
  }
  return matched;
}

function missingRubricPoints(matchedPoints: string[], rubric: EvaluateRequest['rubric']) {
  const matched = new Set(matchedPoints);
  return rubric.map((point) => point.point).filter((point) => !matched.has(point));
}

function scoreFromMatchedPoints(matchedPoints: string[], rubric: EvaluateRequest['rubric']) {
  const matched = new Set(matchedPoints);
  return rubric.reduce((total, point) => total + (matched.has(point.point) ? point.weight : 0), 0);
}

function verdictFor(score: number, maxScore: number) {
  if (score >= maxScore) return 'correct';
  if (score <= 0) return 'incorrect';
  return 'partial';
}
