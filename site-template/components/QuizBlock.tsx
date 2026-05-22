"use client";

import { useState } from "react";

type Citation = {
  ref_id: string;
  quote?: string;
};

type QuizItem = {
  id: string;
  question: string;
  choices: string[];
  answer: string;
  explanation: string;
  citations?: Citation[];
};

export function QuizBlock({ items }: { items: QuizItem[] }) {
  const [selected, setSelected] = useState<Record<string, string>>({});
  const [checked, setChecked] = useState<Record<string, boolean>>({});

  return (
    <section className="quiz-block">
      {items.map((item, index) => {
        const chosen = selected[item.id];
        const isChecked = checked[item.id];
        const isCorrect = chosen === item.answer;

        return (
          <article className="quiz-item" key={item.id}>
            <div className="quiz-title">
              <span>{index + 1}</span>
              <h3>{item.question}</h3>
            </div>
            <div className="quiz-options">
              {item.choices.map((choice) => (
                <button
                  className={choice === chosen ? "quiz-option selected" : "quiz-option"}
                  key={choice}
                  onClick={() => {
                    setSelected((current) => ({ ...current, [item.id]: choice }));
                    setChecked((current) => ({ ...current, [item.id]: false }));
                  }}
                  type="button"
                >
                  {choice}
                </button>
              ))}
            </div>
            <button
              className="quiz-check"
              disabled={!chosen}
              onClick={() => setChecked((current) => ({ ...current, [item.id]: true }))}
              type="button"
            >
              Check
            </button>
            {isChecked ? (
              <div className={isCorrect ? "quiz-feedback correct" : "quiz-feedback wrong"}>
                <strong>{isCorrect ? "Correct" : `Answer: ${item.answer}`}</strong>
                <p>{item.explanation}</p>
              </div>
            ) : null}
          </article>
        );
      })}
    </section>
  );
}
