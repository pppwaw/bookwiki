'use client';

import { useState } from 'react';
import { MathText } from './MathText';

export function SourceRef({ id, quote }: { id: string; quote?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <span
      className="source-ref"
      tabIndex={0}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
      title={quote}
    >
      <span className="source-ref-id">{id}</span>
      {quote && open ? (
        <span className="source-ref-tooltip">
          <MathText text={quote} />
        </span>
      ) : null}
    </span>
  );
}
