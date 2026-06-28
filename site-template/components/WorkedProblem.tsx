'use client';

import './WorkedProblem.css';

import { useMemo, useState, type FormEvent } from 'react';
import { Markdown } from './markdown';
import { postEvaluation, type EvaluationResult, type PointScore } from '@/lib/evaluate-client';

type Citation = {
  ref_id: string;
  quote?: string;
};

type RubricPoint = {
  point: string;
  weight: number;
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
      const evaluation = await postEvaluation({
        chapter_id: chapterId || undefined,
        question,
        reference_answer: referenceAnswer,
        rubric,
        user_answer: userAnswer,
      });
      setResult(evaluation);
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
      <PointScoreList points={result.points} />
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
                  <Markdown inline text={cite.quote} />
                </span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function PointScoreList({ points }: { points: PointScore[] }) {
  if (points.length === 0) return null;
  return (
    <div className="worked-points">
      <h4>逐点得分</h4>
      <ul>
        {points.map((point, index) => (
          <li className={`worked-point worked-point-${pointTone(point)}`} key={`${index}:${point.point}`}>
            <span className="worked-point-score">
              {formatScore(point.earned)} / {formatScore(point.weight)}
            </span>
            <span className="worked-point-text">
              <Markdown inline text={point.point} />
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function pointTone(point: PointScore): 'full' | 'partial' | 'none' {
  if (point.earned >= point.weight) return 'full';
  if (point.earned <= 0) return 'none';
  return 'partial';
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
      <Markdown text={text} />
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
