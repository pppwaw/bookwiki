'use client';

import {
  createContext,
  type ReactNode,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';

type Citation = {
  ref_id: string;
  quote?: string;
};

type QuizContextValue = {
  answer: string;
  answerContent?: ReactNode;
  check: () => void;
  checked: boolean;
  registerChoice: (id: string, children: ReactNode) => void;
  selectChoice: (id: string) => void;
  selected?: string;
};

const QuizContext = createContext<QuizContextValue | null>(null);

export function QuizBlock({ children }: { children: ReactNode }) {
  return <section className="quiz-block">{children}</section>;
}

export function QuizItem({
  answer,
  children,
  id,
}: {
  answer: string;
  children: ReactNode;
  citations?: Citation[];
  id: string;
}) {
  const [selected, setSelected] = useState<string>();
  const [checked, setChecked] = useState(false);
  const [choiceContent, setChoiceContent] = useState<Record<string, ReactNode>>({});

  const value = useMemo<QuizContextValue>(
    () => ({
      answer,
      answerContent: choiceContent[answer],
      check() {
        setChecked(true);
      },
      checked,
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
    [answer, checked, choiceContent, selected],
  );

  return (
    <QuizContext.Provider value={value}>
      <article className="quiz-item" id={id}>
        {children}
      </article>
    </QuizContext.Provider>
  );
}

export function QuizQuestion({ children }: { children: ReactNode }) {
  return (
    <div className="quiz-title">
      <span aria-hidden="true" />
      <h3>{children}</h3>
    </div>
  );
}

export function QuizChoices({ children }: { children: ReactNode }) {
  return <div className="quiz-options">{children}</div>;
}

export function QuizChoice({ children, id }: { children: ReactNode; id: string }) {
  const quiz = useQuizContext();

  useEffect(() => {
    quiz.registerChoice(id, children);
  }, [id, quiz, children]);

  return (
    <button
      className={id === quiz.selected ? 'quiz-option selected' : 'quiz-option'}
      onClick={() => quiz.selectChoice(id)}
      type="button"
    >
      {children}
    </button>
  );
}

export function QuizCheck() {
  const quiz = useQuizContext();

  return (
    <button className="quiz-check" disabled={!quiz.selected} onClick={quiz.check} type="button">
      Check
    </button>
  );
}

export function QuizExplanation({ children }: { children: ReactNode }) {
  const quiz = useQuizContext();
  if (!quiz.checked) return null;

  const isCorrect = quiz.selected === quiz.answer;

  return (
    <div className={isCorrect ? 'quiz-feedback correct' : 'quiz-feedback wrong'}>
      <strong>
        {isCorrect ? 'Correct' : 'Answer: '}
        {isCorrect ? null : (quiz.answerContent ?? quiz.answer)}
      </strong>
      <div>{children}</div>
    </div>
  );
}

function useQuizContext(): QuizContextValue {
  const value = useContext(QuizContext);
  if (!value) {
    throw new Error('Quiz subcomponents must be rendered inside QuizItem');
  }
  return value;
}
