'use client';

import './SourceRef.css';

import Link from 'next/link';
import { useState } from 'react';
import { MathText } from './MathText';

export function SourceRef({ id, quote, href }: { id: string; quote?: string; href?: string }) {
  // Page citations carry an href and render as a clickable link to the cited
  // BookWiki page; plain source_ref markers keep the hover-quote pill behavior.
  // Split into two components so each calls its hooks unconditionally.
  return href ? (
    <SourceRefLink id={id} href={href} quote={quote} />
  ) : (
    <SourceRefQuote id={id} quote={quote} />
  );
}

function SourceRefLink({ id, href, quote }: { id: string; href: string; quote?: string }) {
  return (
    <Link className="source-ref source-ref-link" href={href} aria-label={quote ?? id}>
      <span className="source-ref-id">{id}</span>
    </Link>
  );
}

function SourceRefQuote({ id, quote }: { id: string; quote?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <span
      className="source-ref"
      tabIndex={0}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
      aria-label={quote}
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
