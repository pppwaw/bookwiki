// Client helper for the streaming /api/evaluate endpoint.
//
// The endpoint streams NDJSON: zero or more `{ "type": "progress" }` heartbeat
// lines (emitted as the grader reasons/generates, so the connection never sits
// idle and long judging runs don't get killed by an upstream timeout) followed
// by a terminal `{ "type": "result", result }` or `{ "type": "error", error }`.
// The progress lines are intentionally content-free — callers only show a
// "judging" indicator while draining the stream and act on the final line.

export type PointScore = {
  point: string;
  earned: number;
  weight: number;
};

export type EvaluationResult = {
  verdict: 'correct' | 'partial' | 'incorrect';
  score: number;
  max_score: number;
  points: PointScore[];
  feedback: string;
  revised_answer: string;
};

type EvaluateEvent =
  | { type: 'progress' }
  | { type: 'result'; result: EvaluationResult }
  | { type: 'error'; error: string };

export async function postEvaluation(body: unknown, signal?: AbortSignal): Promise<EvaluationResult> {
  const response = await fetch('/api/evaluate', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });

  // Validation/auth failures short-circuit before streaming starts and reply
  // with a plain JSON error body.
  if (!response.ok || !response.body) {
    let message = '判分失败';
    try {
      const payload = (await response.json()) as { error?: string };
      if (payload?.error) message = payload.error;
    } catch {
      // non-JSON body — keep the default message
    }
    throw new Error(message);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let final: EvaluationResult | null = null;

  for (;;) {
    const { done, value } = await reader.read();
    if (value) buffer += decoder.decode(value, { stream: true });

    let newline: number;
    while ((newline = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (!line) continue;

      const event = JSON.parse(line) as EvaluateEvent;
      if (event.type === 'error') throw new Error(event.error || '判分失败');
      if (event.type === 'result') final = event.result;
      // 'progress' heartbeats are ignored
    }

    if (done) break;
  }

  if (!final) throw new Error('判分失败');
  return final;
}
