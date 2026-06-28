'use client';

import './WorkedProblem.css';

import { useMemo, useState, type FormEvent } from 'react';
import { MathText } from './MathText';

type Citation = {
  ref_id: string;
  quote?: string;
};

type RubricPoint = {
  point: string;
  weight: number;
};

type EvaluationResult = {
  verdict: 'correct' | 'partial' | 'incorrect';
  score: number;
  max_score: number;
  matched_points: string[];
  missing_points: string[];
  feedback: string;
  revised_answer: string;
};

export function WorkedProblem({
  chapterId,
  citations = [],
  explanation = '',
  id,
  question,
  referenceAnswer,
  rubric,
}: {
  chapterId?: string;
  citations?: Citation[];
  explanation?: string;
  id: string;
  question: string;
  referenceAnswer: string;
  rubric: RubricPoint[];
}) {
  const [answer, setAnswer] = useState('');
  const [result, setResult] = useState<EvaluationResult | null>(null);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const maxScore = useMemo(() => rubric.reduce((total, point) => total + point.weight, 0), [rubric]);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const userAnswer = answer.trim();
    if (!userAnswer) return;

    setSubmitting(true);
    setError('');
    setResult(null);
    try {
      const response = await fetch('/api/evaluate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          chapter_id: chapterId || undefined,
          question,
          reference_answer: referenceAnswer,
          rubric,
          user_answer: userAnswer,
        }),
      });
      const payload = (await response.json()) as EvaluationResult | { error?: string };
      if (!response.ok) {
        setError('error' in payload && payload.error ? payload.error : '判分失败');
        return;
      }
      setResult(payload as EvaluationResult);
    } catch (err) {
      setError(err instanceof Error ? err.message : '判分失败');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <article className="worked-problem" id={id}>
      <div className="worked-problem-head">
        <span className="worked-problem-label">Worked problem</span>
        <span className="worked-problem-score">满分 {formatScore(maxScore)}</span>
      </div>
      <section className="worked-problem-question">
        <h3>题目</h3>
        <TextBlock text={question} />
        {explanation ? (
          <details className="worked-problem-hint">
            <summary>提示</summary>
            <TextBlock text={explanation} />
          </details>
        ) : null}
      </section>
      <form className="worked-problem-form" onSubmit={submit}>
        <label htmlFor={`${id}-answer`}>你的解题过程</label>
        <textarea
          id={`${id}-answer`}
          onChange={(event) => setAnswer(event.target.value)}
          placeholder="写下证明或计算过程。可使用 $...$ / $$...$$ 输入公式。"
          rows={8}
          value={answer}
        />
        <div className="worked-problem-actions">
          <button disabled={!answer.trim() || submitting} type="submit">
            {submitting ? '判分中…' : '提交判分'}
          </button>
          {(answer || result || error) && (
            <button
              onClick={() => {
                setAnswer('');
                setResult(null);
                setError('');
              }}
              type="button"
            >
              重置
            </button>
          )}
        </div>
      </form>
      {error ? <div className="worked-problem-error">{error}</div> : null}
      {result ? (
        <EvaluationPanel citations={citations} referenceAnswer={referenceAnswer} result={result} />
      ) : null}
    </article>
  );
}

function EvaluationPanel({
  citations,
  referenceAnswer,
  result,
}: {
  citations: Citation[];
  referenceAnswer: string;
  result: EvaluationResult;
}) {
  return (
    <section className={`worked-evaluation worked-evaluation-${result.verdict}`} aria-live="polite">
      <div className="worked-evaluation-summary">
        <strong>{verdictLabel(result.verdict)}</strong>
        <span>
          {formatScore(result.score)} / {formatScore(result.max_score)}
        </span>
      </div>
      <TextBlock text={result.feedback} />
      <PointList className="worked-points-matched" label="命中要点" points={result.matched_points} />
      <PointList className="worked-points-missing" label="缺漏要点" points={result.missing_points} />
      <AnswerBlock title="修补后的答案" text={result.revised_answer} />
      <AnswerBlock title="完整参考答案" text={referenceAnswer} />
      {citations.length > 0 ? (
        <ul className="worked-citations" aria-label="Sources">
          {citations.map((cite) => (
            <li key={cite.ref_id}>
              <code>{cite.ref_id}</code>
              {cite.quote ? (
                <span>
                  {' · '}
                  <MathText text={cite.quote} />
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function PointList({ className, label, points }: { className: string; label: string; points: string[] }) {
  if (points.length === 0) return null;
  return (
    <div className={className}>
      <h4>{label}</h4>
      <ul>
        {points.map((point, index) => (
          <li key={`${index}:${point}`}>
            <MathText text={point} />
          </li>
        ))}
      </ul>
    </div>
  );
}

function AnswerBlock({ text, title }: { text: string; title: string }) {
  return (
    <details className="worked-answer-block" open={title === '修补后的答案'}>
      <summary>{title}</summary>
      <TextBlock text={text} />
    </details>
  );
}

function TextBlock({ className = '', text }: { className?: string; text: string }) {
  return (
    <div className={className || undefined}>
      {text
        .split(/\n{2,}/)
        .map((part) => part.trim())
        .filter(Boolean)
        .map((paragraph, index) => (
          <p key={`${index}:${paragraph}`}>
            <MathText text={paragraph} />
          </p>
        ))}
    </div>
  );
}

function verdictLabel(verdict: EvaluationResult['verdict']) {
  if (verdict === 'correct') return '正确';
  if (verdict === 'incorrect') return '错误';
  return '部分正确';
}

function formatScore(value: number) {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}
