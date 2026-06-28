'use client';

import './ExamBlock.css';

import {
  createContext,
  Fragment,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import { MathText } from './MathText';
import { Markdown } from './markdown';
import { postEvaluation, type EvaluationResult, type PointScore } from '@/lib/evaluate-client';

// Chapter-end exam + past-paper walkthrough.
//
// `mode="exam"`   — whole paper shown at once, NO per-question feedback while answering;
//                   one "提交" grades everything: choice/fill locally, worked via /api/evaluate.
// `mode="walkthrough"` — review an actual past paper: each question shows a foldable concept
//                   recap and a foldable full solution (question-first pedagogy).
//
// Threat model is a student revising, not cheating, so answers live in the page; "don't reveal
// until submit" is a courtesy, not a security boundary.

type ExamMode = 'exam' | 'walkthrough';

type RubricPoint = { point: string; weight: number };

type ItemScore = { earned: number; max: number; pending: boolean };

type DeckState = {
  mode: ExamMode;
  submitted: boolean;
  resetToken: number;
  register: (id: string, max: number) => void;
  report: (id: string, score: ItemScore) => void;
};

const DeckContext = createContext<DeckState | null>(null);

function useDeck(): DeckState {
  const deck = useContext(DeckContext);
  if (!deck) throw new Error('Exam subcomponents must be rendered inside <ExamBlock>');
  return deck;
}

export function ExamBlock({
  chapterId: _chapterId,
  children,
  mode = 'exam',
}: {
  chapterId?: string;
  children: ReactNode;
  mode?: ExamMode;
}) {
  const [submitted, setSubmitted] = useState(mode === 'walkthrough');
  const [resetToken, setResetToken] = useState(0);
  const [maxById, setMaxById] = useState<Map<string, number>>(new Map());
  const [scoreById, setScoreById] = useState<Map<string, ItemScore>>(new Map());

  const register = useCallback((id: string, max: number) => {
    setMaxById((current) => {
      if (current.get(id) === max) return current;
      const next = new Map(current);
      next.set(id, max);
      return next;
    });
  }, []);

  const report = useCallback((id: string, score: ItemScore) => {
    setScoreById((current) => {
      const next = new Map(current);
      next.set(id, score);
      return next;
    });
  }, []);

  const reset = useCallback(() => {
    setSubmitted(false);
    setScoreById(new Map());
    setResetToken((token) => token + 1);
  }, []);

  const value = useMemo<DeckState>(
    () => ({ mode, submitted, resetToken, register, report }),
    [mode, submitted, resetToken, register, report],
  );

  const total = maxById.size;
  const maxScore = Array.from(maxById.values()).reduce((sum, max) => sum + max, 0);
  const scores = Array.from(scoreById.values());
  const earned = scores.reduce((sum, score) => sum + score.earned, 0);
  const pending = scores.some((score) => score.pending);

  return (
    <DeckContext.Provider value={value}>
      <section className="exam-block" aria-label="Exam">
        {mode === 'exam' ? (
          <header className="exam-block-head">
            <span className="exam-block-label">章末考试 · 共 {total} 题</span>
            {!submitted ? (
              <button
                className="exam-submit"
                type="button"
                disabled={total === 0}
                onClick={() => setSubmitted(true)}
              >
                提交并判分
              </button>
            ) : (
              <div className="exam-score" role="status" aria-live="polite">
                <strong>
                  得分 {formatScore(earned)} / {formatScore(maxScore)}
                </strong>
                {pending ? <span className="exam-score-pending"> · 大题判分中…</span> : null}
                <button className="exam-reset" type="button" onClick={reset}>
                  重做
                </button>
              </div>
            )}
          </header>
        ) : null}
        {children}
      </section>
    </DeckContext.Provider>
  );
}

// --- per-question shell ----------------------------------------------------

type ItemContextValue = {
  mode: ExamMode;
  submitted: boolean;
  resetToken: number;
  type: string;
  answerIds: string[];
  selected: Set<string>;
  toggle: (choiceId: string) => void;
};

const ItemContext = createContext<ItemContextValue | null>(null);

function useItem(): ItemContextValue {
  const value = useContext(ItemContext);
  if (!value) throw new Error('Exam question parts must be rendered inside <ExamItem>');
  return value;
}

export function ExamItem({
  acceptedAnswers,
  answer = [],
  children,
  id,
  referenceAnswer = '',
  rubric = [],
  type,
}: {
  acceptedAnswers?: string[][];
  answer?: string[];
  children: ReactNode;
  fromExam?: boolean;
  id: string;
  referenceAnswer?: string;
  rubric?: RubricPoint[];
  type: 'single_choice' | 'multiple_choice' | 'fill_blank' | 'worked';
}) {
  const deck = useDeck();
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const maxScore = useMemo(() => {
    if (type === 'worked') return rubric.reduce((sum, point) => sum + point.weight, 0) || 1;
    return 1;
  }, [type, rubric]);

  useEffect(() => {
    deck.register(id, maxScore);
  }, [deck, id, maxScore]);

  useEffect(() => {
    setSelected(new Set());
  }, [deck.resetToken]);

  const toggle = useCallback(
    (choiceId: string) => {
      if (deck.submitted && deck.mode === 'exam') return;
      setSelected((current) => {
        const next = new Set(type === 'multiple_choice' ? current : []);
        if (current.has(choiceId) && type === 'multiple_choice') next.delete(choiceId);
        else next.add(choiceId);
        return next;
      });
    },
    [deck.submitted, deck.mode, type],
  );

  // Choice grading is local and synchronous; report whenever submitted.
  useEffect(() => {
    if (!deck.submitted) return;
    if (type !== 'single_choice' && type !== 'multiple_choice') return;
    const answerSet = new Set(answer);
    const correct = answerSet.size === selected.size && [...answerSet].every((a) => selected.has(a));
    deck.report(id, { earned: correct ? 1 : 0, max: 1, pending: false });
  }, [deck, id, type, answer, selected]);

  const itemValue = useMemo<ItemContextValue>(
    () => ({
      mode: deck.mode,
      submitted: deck.submitted,
      resetToken: deck.resetToken,
      type,
      answerIds: answer,
      selected,
      toggle,
    }),
    [deck.mode, deck.submitted, deck.resetToken, type, answer, selected, toggle],
  );

  return (
    <ItemContext.Provider value={itemValue}>
      <article className="exam-item" id={id} data-type={type}>
        {children}
        {type === 'fill_blank' ? (
          <FillBlankInputs id={id} acceptedAnswers={acceptedAnswers ?? []} />
        ) : null}
        {type === 'worked' ? (
          <WorkedAnswer id={id} referenceAnswer={referenceAnswer} rubric={rubric} />
        ) : null}
      </article>
    </ItemContext.Provider>
  );
}

export function ExamQuestion({ children }: { children: ReactNode }) {
  return (
    <div className="exam-question">
      <div className="exam-question__body">
        <MathContent>{children}</MathContent>
      </div>
    </div>
  );
}

export function ExamChoices({ children }: { children: ReactNode }) {
  const item = useItem();
  return (
    <div className="exam-options" role={item.type === 'multiple_choice' ? 'group' : 'radiogroup'}>
      {children}
    </div>
  );
}

export function ExamChoice({ children, id }: { children: ReactNode; id: string }) {
  const item = useItem();
  const isSelected = item.selected.has(id);
  const reveal = item.submitted || item.mode === 'walkthrough';
  const isAnswer = item.answerIds.includes(id);

  return (
    <button
      type="button"
      className={[
        'exam-option',
        isSelected ? 'selected' : '',
        reveal && isAnswer ? 'is-answer' : '',
        reveal && isSelected && !isAnswer ? 'is-wrong' : '',
      ]
        .filter(Boolean)
        .join(' ')}
      aria-pressed={isSelected}
      onClick={() => item.toggle(id)}
    >
      <span className="exam-option-marker" aria-hidden="true">
        {reveal && isAnswer ? '✓' : reveal && isSelected && !isAnswer ? '✕' : ''}
      </span>
      <span className="exam-option-body">
        <MathContent>{children}</MathContent>
      </span>
    </button>
  );
}

export function ExamConceptRecap({ children }: { children: ReactNode }) {
  // Foldable knowledge refresh — collapsed by default so the question comes first.
  return (
    <details className="exam-recap">
      <summary>相关知识点回顾</summary>
      <div className="exam-recap-body">
        <MathContent>{children}</MathContent>
      </div>
    </details>
  );
}

export function ExamExplanation({ children }: { children: ReactNode }) {
  const item = useItem();
  if (item.mode === 'exam' && !item.submitted) return null;
  return (
    <div className="exam-explanation" role="status">
      <MathContent>{children}</MathContent>
    </div>
  );
}

// --- fill blank ------------------------------------------------------------

function FillBlankInputs({ acceptedAnswers, id }: { acceptedAnswers: string[][]; id: string }) {
  const deck = useDeck();
  const [values, setValues] = useState<string[]>(() => acceptedAnswers.map(() => ''));

  useEffect(() => {
    setValues(acceptedAnswers.map(() => ''));
  }, [deck.resetToken, acceptedAnswers]);

  useEffect(() => {
    if (!deck.submitted) return;
    const correct = acceptedAnswers.reduce((count, group, index) => {
      const candidate = normalize(values[index] ?? '');
      const ok = group.some((accepted) => normalize(accepted) === candidate);
      return count + (ok ? 1 : 0);
    }, 0);
    const earned = acceptedAnswers.length === 0 ? 0 : correct / acceptedAnswers.length;
    deck.report(id, { earned, max: 1, pending: false });
  }, [deck, id, acceptedAnswers, values]);

  const reveal = deck.submitted || deck.mode === 'walkthrough';

  return (
    <div className="exam-fill">
      {acceptedAnswers.map((group, index) => {
        const candidate = normalize(values[index] ?? '');
        const ok = group.some((accepted) => normalize(accepted) === candidate);
        return (
          <div className="exam-fill-row" key={index}>
            <label htmlFor={`${id}-blank-${index}`}>空 {index + 1}</label>
            <input
              id={`${id}-blank-${index}`}
              type="text"
              value={values[index] ?? ''}
              disabled={reveal && deck.mode === 'exam'}
              onChange={(event) =>
                setValues((current) => {
                  const next = [...current];
                  next[index] = event.target.value;
                  return next;
                })
              }
            />
            {reveal ? (
              <span className={ok ? 'exam-fill-ok' : 'exam-fill-bad'}>
                {ok ? '✓' : '✕'} 参考：<MathText text={group.join(' / ')} />
              </span>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

// --- worked ----------------------------------------------------------------

function WorkedAnswer({
  id,
  referenceAnswer,
  rubric,
}: {
  id: string;
  referenceAnswer: string;
  rubric: RubricPoint[];
}) {
  const deck = useDeck();
  const item = useItem();
  const [answer, setAnswer] = useState('');
  const [result, setResult] = useState<EvaluationResult | null>(null);
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    setAnswer('');
    setResult(null);
    setError('');
  }, [deck.resetToken]);

  const grade = useCallback(async () => {
    const userAnswer = answer.trim();
    const maxScore = rubric.reduce((sum, point) => sum + point.weight, 0) || 1;
    if (!userAnswer) {
      deck.report(id, { earned: 0, max: maxScore, pending: false });
      return;
    }
    setSubmitting(true);
    deck.report(id, { earned: 0, max: maxScore, pending: true });
    try {
      const evaluation = await postEvaluation({
        question: item.type,
        reference_answer: referenceAnswer,
        rubric,
        user_answer: userAnswer,
      });
      setResult(evaluation);
      deck.report(id, { earned: evaluation.score, max: evaluation.max_score, pending: false });
    } catch (err) {
      setError(err instanceof Error ? err.message : '判分失败');
      deck.report(id, { earned: 0, max: maxScore, pending: false });
    } finally {
      setSubmitting(false);
    }
  }, [answer, deck, id, item.type, referenceAnswer, rubric]);

  // In exam mode the deck-level submit triggers grading once.
  useEffect(() => {
    if (deck.mode === 'exam' && deck.submitted && !result && !submitting) {
      void grade();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [deck.submitted]);

  const reveal = deck.submitted || deck.mode === 'walkthrough';

  return (
    <div className="exam-worked">
      <label htmlFor={`${id}-worked`}>你的解题过程</label>
      <textarea
        id={`${id}-worked`}
        rows={6}
        value={answer}
        disabled={deck.mode === 'exam' && deck.submitted}
        placeholder="写下证明或计算过程，可用 $...$ / $$...$$。"
        onChange={(event) => setAnswer(event.target.value)}
      />
      {deck.mode === 'walkthrough' ? (
        <button type="button" disabled={!answer.trim() || submitting} onClick={() => void grade()}>
          {submitting ? '判分中…' : '自测判分'}
        </button>
      ) : null}
      {error ? <div className="exam-worked-error">{error}</div> : null}
      {result ? (
        <div className={`exam-worked-result exam-worked-${result.verdict}`} aria-live="polite">
          <strong>
            {verdictLabel(result.verdict)} · {formatScore(result.score)} / {formatScore(result.max_score)}
          </strong>
          <Paragraphs text={result.feedback} />
          <PointScoreList points={result.points} />
        </div>
      ) : null}
      {reveal ? (
        <details className="exam-reference" open={!result}>
          <summary>完整参考答案与解析</summary>
          <Paragraphs text={referenceAnswer} />
        </details>
      ) : null}
    </div>
  );
}

// --- shared helpers --------------------------------------------------------

function PointScoreList({ points }: { points: PointScore[] }) {
  if (points.length === 0) return null;
  return (
    <div className="exam-points">
      <h4>逐点得分</h4>
      <ul>
        {points.map((point, index) => (
          <li className={`exam-point exam-point-${pointTone(point)}`} key={`${index}:${point.point}`}>
            <span className="exam-point-score">
              {formatScore(point.earned)} / {formatScore(point.weight)}
            </span>
            <span className="exam-point-text">
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

function Paragraphs({ text }: { text: string }) {
  return <Markdown text={text} />;
}

function MathContent({ children }: { children: ReactNode }): ReactNode {
  if (typeof children === 'string') return <MathText text={children} />;
  if (Array.isArray(children)) {
    return children.map((child, index) => <Fragment key={index}>{MathContent({ children: child })}</Fragment>);
  }
  return children;
}

function normalize(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/\s+/g, '')
    .replace(/[！-～]/g, (char) => String.fromCharCode(char.charCodeAt(0) - 0xfee0));
}

function verdictLabel(verdict: EvaluationResult['verdict']): string {
  if (verdict === 'correct') return '正确';
  if (verdict === 'incorrect') return '错误';
  return '部分正确';
}

function formatScore(value: number): string {
  return Number.isInteger(value) ? String(value) : value.toFixed(1);
}
