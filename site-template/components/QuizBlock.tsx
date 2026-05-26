'use client';

import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';

type Citation = {
  ref_id: string;
  quote?: string;
};

type ItemRecord = {
  id: string;
  correct: boolean | null;
};

type DeckState = {
  records: Map<string, ItemRecord>;
  setRecord: (id: string, value: ItemRecord) => void;
  reset: () => void;
  resetToken: number;
  total: number;
  registerItem: (id: string) => void;
};

const DeckContext = createContext<DeckState | null>(null);

type ItemContextValue = {
  answer: string;
  answerContent?: ReactNode;
  citations: Citation[];
  check: () => void;
  reset: () => void;
  checked: boolean;
  correct: boolean | null;
  registerChoice: (id: string, children: ReactNode) => void;
  selectChoice: (id: string) => void;
  selected?: string;
};

const ItemContext = createContext<ItemContextValue | null>(null);

export function QuizBlock({ children }: { children: ReactNode }) {
  const [records, setRecords] = useState<Map<string, ItemRecord>>(new Map());
  const [resetToken, setResetToken] = useState(0);
  const [registered, setRegistered] = useState<Set<string>>(new Set());

  const setRecord = useCallback((id: string, value: ItemRecord) => {
    setRecords((current) => {
      const next = new Map(current);
      next.set(id, value);
      return next;
    });
  }, []);

  const reset = useCallback(() => {
    setRecords(new Map());
    setResetToken((token) => token + 1);
  }, []);

  const registerItem = useCallback((id: string) => {
    setRegistered((current) => {
      if (current.has(id)) return current;
      const next = new Set(current);
      next.add(id);
      return next;
    });
  }, []);

  const value = useMemo<DeckState>(
    () => ({
      records,
      setRecord,
      reset,
      resetToken,
      total: registered.size,
      registerItem,
    }),
    [records, setRecord, reset, resetToken, registered, registerItem],
  );

  const answered = Array.from(records.values()).filter((rec) => rec.correct !== null);
  const correctCount = answered.filter((rec) => rec.correct).length;
  const total = registered.size;
  const percent = total === 0 ? 0 : Math.round((answered.length / total) * 100);

  return (
    <DeckContext.Provider value={value}>
      <section className="quiz-block" aria-label="Quiz">
        <header className="quiz-block-head">
          <div className="quiz-progress" role="status" aria-live="polite">
            <span className="quiz-progress-label">
              {answered.length} / {total} answered · {correctCount} correct
            </span>
            <div className="quiz-progress-bar" aria-hidden="true">
              <div className="quiz-progress-bar-fill" style={{ width: `${percent}%` }} />
            </div>
          </div>
          <button
            className="quiz-reset"
            type="button"
            onClick={reset}
            disabled={answered.length === 0}
          >
            Reset
          </button>
        </header>
        {children}
      </section>
    </DeckContext.Provider>
  );
}

export function QuizItem({
  answer,
  children,
  citations,
  id,
}: {
  answer: string;
  children: ReactNode;
  citations?: Citation[];
  id: string;
}) {
  const deck = useContext(DeckContext);
  const [selected, setSelected] = useState<string>();
  const [checked, setChecked] = useState(false);
  const [choiceContent, setChoiceContent] = useState<Record<string, ReactNode>>({});

  useEffect(() => {
    deck?.registerItem(id);
  }, [deck, id]);

  useEffect(() => {
    if (!deck) return;
    setSelected(undefined);
    setChecked(false);
  }, [deck?.resetToken]);

  const correct = checked && selected !== undefined ? selected === answer : null;

  useEffect(() => {
    deck?.setRecord(id, { id, correct });
  }, [deck, id, correct]);

  const value = useMemo<ItemContextValue>(
    () => ({
      answer,
      answerContent: choiceContent[answer],
      citations: citations ?? [],
      check() {
        setChecked(true);
      },
      reset() {
        setSelected(undefined);
        setChecked(false);
      },
      checked,
      correct,
      registerChoice(choiceId, content) {
        setChoiceContent((current) =>
          Object.prototype.hasOwnProperty.call(current, choiceId)
            ? current
            : { ...current, [choiceId]: content },
        );
      },
      selectChoice(choiceId) {
        setSelected(choiceId);
        setChecked(false);
      },
      selected,
    }),
    [answer, checked, choiceContent, citations, correct, selected],
  );

  return (
    <ItemContext.Provider value={value}>
      <article
        className={
          correct === true
            ? 'quiz-item quiz-item-correct'
            : correct === false
              ? 'quiz-item quiz-item-wrong'
              : 'quiz-item'
        }
        id={id}
      >
        {children}
      </article>
    </ItemContext.Provider>
  );
}

export function QuizQuestion({ children }: { children: ReactNode }) {
  return (
    <div className="quiz-title">
      <span className="quiz-counter" aria-hidden="true" />
      <h3>{children}</h3>
    </div>
  );
}

export function QuizChoices({ children }: { children: ReactNode }) {
  return (
    <div className="quiz-options" role="radiogroup">
      {children}
    </div>
  );
}

export function QuizChoice({ children, id }: { children: ReactNode; id: string }) {
  const quiz = useItemContext();

  useEffect(() => {
    quiz.registerChoice(id, children);
  }, [id, quiz, children]);

  const isSelected = id === quiz.selected;
  const isAnswer = id === quiz.answer;
  const showAnswer = quiz.checked && isAnswer;
  const showWrong = quiz.checked && isSelected && !isAnswer;

  return (
    <button
      className={[
        'quiz-option',
        isSelected ? 'selected' : '',
        showAnswer ? 'is-answer' : '',
        showWrong ? 'is-wrong' : '',
      ]
        .filter(Boolean)
        .join(' ')}
      onClick={() => quiz.selectChoice(id)}
      type="button"
      role="radio"
      aria-checked={isSelected}
    >
      <span className="quiz-option-marker" aria-hidden="true">
        {showAnswer ? '✓' : showWrong ? '✕' : ''}
      </span>
      <span className="quiz-option-body">{children}</span>
    </button>
  );
}

export function QuizCheck() {
  const quiz = useItemContext();

  return (
    <div className="quiz-actions">
      <button
        className="quiz-check"
        disabled={!quiz.selected}
        onClick={quiz.check}
        type="button"
      >
        Check answer
      </button>
      {quiz.checked && (
        <button className="quiz-try-again" onClick={quiz.reset} type="button">
          Try again
        </button>
      )}
    </div>
  );
}

export function QuizExplanation({ children }: { children: ReactNode }) {
  const quiz = useItemContext();
  if (!quiz.checked) return null;

  const isCorrect = quiz.selected === quiz.answer;

  return (
    <div
      className={isCorrect ? 'quiz-feedback correct' : 'quiz-feedback wrong'}
      role="status"
      aria-live="polite"
    >
      <div className="quiz-feedback-headline">
        <strong>{isCorrect ? '✓ Correct' : '✕ Not quite'}</strong>
        {!isCorrect ? (
          <span className="quiz-feedback-answer">
            Answer: {quiz.answerContent ?? quiz.answer}
          </span>
        ) : null}
      </div>
      <div className="quiz-feedback-body">{children}</div>
      {quiz.citations.length > 0 && (
        <ul className="quiz-feedback-citations" aria-label="Sources">
          {quiz.citations.map((cite) => (
            <li key={cite.ref_id}>
              <code>{cite.ref_id}</code>
              {cite.quote ? <span> · {cite.quote}</span> : null}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function useItemContext(): ItemContextValue {
  const value = useContext(ItemContext);
  if (!value) {
    throw new Error('Quiz subcomponents must be rendered inside QuizItem');
  }
  return value;
}
