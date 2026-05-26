'use client';

import {
  createContext,
  type KeyboardEvent,
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

type AnkiContextValue = {
  activeId?: string;
  cardIds: string[];
  currentIndex: number;
  showBack: boolean;
  goTo: (index: number) => void;
  flip: () => void;
  reset: () => void;
};

const AnkiContext = createContext<AnkiContextValue | null>(null);

export function AnkiDeck({
  cardIds = [],
  children,
}: {
  cardIds?: string[];
  children: ReactNode;
}) {
  const [index, setIndex] = useState(0);
  const [showBack, setShowBack] = useState(false);

  const goTo = useCallback(
    (next: number) => {
      const clamped = Math.min(Math.max(next, 0), Math.max(cardIds.length - 1, 0));
      setIndex(clamped);
      setShowBack(false);
    },
    [cardIds.length],
  );

  const flip = useCallback(() => setShowBack((current) => !current), []);
  const reset = useCallback(() => {
    setIndex(0);
    setShowBack(false);
  }, []);

  const activeId = cardIds[index];

  const value = useMemo<AnkiContextValue>(
    () => ({
      activeId,
      cardIds,
      currentIndex: index,
      showBack,
      goTo,
      flip,
      reset,
    }),
    [activeId, cardIds, index, showBack, goTo, flip, reset],
  );

  const handleKey = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      if (event.key === 'ArrowLeft') {
        event.preventDefault();
        goTo(index - 1);
      } else if (event.key === 'ArrowRight') {
        event.preventDefault();
        goTo(index + 1);
      } else if (event.key === ' ' || event.key === 'Enter') {
        if ((event.target as HTMLElement).tagName === 'BUTTON') return;
        event.preventDefault();
        flip();
      }
    },
    [flip, goTo, index],
  );

  if (cardIds.length === 0) {
    return null;
  }

  const percent = Math.round(((index + 1) / cardIds.length) * 100);

  return (
    <AnkiContext.Provider value={value}>
      <section
        className="anki-deck"
        aria-label="Flashcard deck"
        tabIndex={0}
        onKeyDown={handleKey}
      >
        <header className="anki-toolbar">
          <div className="anki-progress">
            <span className="anki-progress-label">
              Card {index + 1} of {cardIds.length}
            </span>
            <div className="anki-progress-bar" aria-hidden="true">
              <div className="anki-progress-bar-fill" style={{ width: `${percent}%` }} />
            </div>
          </div>
          <div className="anki-actions">
            <button
              type="button"
              className="anki-nav"
              onClick={() => goTo(index - 1)}
              disabled={index === 0}
              aria-label="Previous card"
            >
              ←
            </button>
            <button
              type="button"
              className="anki-nav"
              onClick={() => goTo(index + 1)}
              disabled={index === cardIds.length - 1}
              aria-label="Next card"
            >
              →
            </button>
            <button
              type="button"
              className="anki-reset"
              onClick={reset}
              disabled={index === 0 && !showBack}
            >
              Reset
            </button>
          </div>
        </header>
        {children}
        <p className="anki-hint">
          Click the card to flip · ← → to move · Space to flip
        </p>
      </section>
    </AnkiContext.Provider>
  );
}

export function AnkiCard({
  children,
  citations,
  id,
}: {
  children: ReactNode;
  citations?: Citation[];
  id: string;
}) {
  const deck = useAnkiContext();
  if (deck.activeId !== id) return null;

  return (
    <div className="anki-stage">
      <button
        className={deck.showBack ? 'anki-card flipped' : 'anki-card'}
        onClick={deck.flip}
        type="button"
        aria-pressed={deck.showBack}
        aria-label={deck.showBack ? 'Show front of card' : 'Show back of card'}
      >
        <div className="anki-card-inner">
          <div className="anki-card-face anki-card-front">
            <span className="anki-face-tag">Front</span>
            <div className="anki-face-body">{children}</div>
          </div>
        </div>
      </button>
      {citations && citations.length > 0 && (
        <ul className="anki-citations" aria-label="Sources">
          {citations.map((cite) => (
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

export function AnkiFront({ children }: { children: ReactNode }) {
  const deck = useAnkiContext();
  return deck.showBack ? null : <div className="anki-face-content">{children}</div>;
}

export function AnkiBack({ children }: { children: ReactNode }) {
  const deck = useAnkiContext();
  return deck.showBack ? <div className="anki-face-content anki-face-back-content">{children}</div> : null;
}

function useAnkiContext(): AnkiContextValue {
  const value = useContext(AnkiContext);
  if (!value) {
    throw new Error('Anki subcomponents must be rendered inside AnkiDeck');
  }
  return value;
}
