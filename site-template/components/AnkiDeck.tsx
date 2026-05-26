'use client';

import { createContext, type ReactNode, useContext, useMemo, useState } from 'react';

type Citation = {
  ref_id: string;
  quote?: string;
};

type AnkiContextValue = {
  activeId?: string;
  cardIds: string[];
  currentIndex: number;
  showBack: boolean;
  setCurrentIndex: (index: number) => void;
  setShowBack: (value: boolean | ((current: boolean) => boolean)) => void;
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
  const activeId = cardIds[index];
  const value = useMemo<AnkiContextValue>(
    () => ({
      activeId,
      cardIds,
      currentIndex: index,
      showBack,
      setCurrentIndex(nextIndex) {
        setIndex(Math.min(Math.max(nextIndex, 0), Math.max(cardIds.length - 1, 0)));
        setShowBack(false);
      },
      setShowBack,
    }),
    [activeId, cardIds, index, showBack],
  );

  if (cardIds.length === 0) {
    return null;
  }

  return (
    <AnkiContext.Provider value={value}>
      <section className="anki-deck">
        <div className="anki-toolbar">
          <span>
            {index + 1} / {cardIds.length}
          </span>
          <div>
            <button
              disabled={index === 0}
              onClick={() => value.setCurrentIndex(index - 1)}
              type="button"
            >
              Previous
            </button>
            <button
              disabled={index === cardIds.length - 1}
              onClick={() => value.setCurrentIndex(index + 1)}
              type="button"
            >
              Next
            </button>
          </div>
        </div>
        {children}
      </section>
    </AnkiContext.Provider>
  );
}

export function AnkiCard({
  children,
  id,
}: {
  children: ReactNode;
  citations?: Citation[];
  id: string;
}) {
  const deck = useAnkiContext();
  if (deck.activeId !== id) return null;

  return (
    <button
      className="anki-card"
      onClick={() => deck.setShowBack((current) => !current)}
      type="button"
    >
      <span>{children}</span>
    </button>
  );
}

export function AnkiFront({ children }: { children: ReactNode }) {
  const deck = useAnkiContext();
  return deck.showBack ? null : <>{children}</>;
}

export function AnkiBack({ children }: { children: ReactNode }) {
  const deck = useAnkiContext();
  return deck.showBack ? <>{children}</> : null;
}

function useAnkiContext(): AnkiContextValue {
  const value = useContext(AnkiContext);
  if (!value) {
    throw new Error('Anki subcomponents must be rendered inside AnkiDeck');
  }
  return value;
}
