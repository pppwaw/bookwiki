'use client';

import { useState } from 'react';

type Citation = {
  ref_id: string;
  quote?: string;
};

type AnkiCard = {
  id: string;
  front: string;
  back: string;
  citations?: Citation[];
};

export function AnkiDeck({ cards }: { cards: AnkiCard[] }) {
  const [index, setIndex] = useState(0);
  const [showBack, setShowBack] = useState(false);
  const card = cards[index];

  if (!card) {
    return null;
  }

  return (
    <section className="anki-deck">
      <div className="anki-toolbar">
        <span>
          {index + 1} / {cards.length}
        </span>
        <div>
          <button
            disabled={index === 0}
            onClick={() => {
              setIndex((current) => Math.max(current - 1, 0));
              setShowBack(false);
            }}
            type="button"
          >
            Previous
          </button>
          <button
            disabled={index === cards.length - 1}
            onClick={() => {
              setIndex((current) => Math.min(current + 1, cards.length - 1));
              setShowBack(false);
            }}
            type="button"
          >
            Next
          </button>
        </div>
      </div>
      <button className="anki-card" onClick={() => setShowBack((value) => !value)} type="button">
        <span>{showBack ? card.back : card.front}</span>
      </button>
    </section>
  );
}
