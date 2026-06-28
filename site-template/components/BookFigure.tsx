'use client';

import './BookFigure.css';

import { useState } from 'react';
import { SourceRef } from './SourceRef';

export function BookFigure({
  id,
  src,
  sourceRef,
  caption,
}: {
  id: string;
  src?: string;
  sourceRef?: string;
  caption?: string;
}) {
  const [failed, setFailed] = useState(false);

  // A figure with no image asset is just a caption-only box that reads as a phantom
  // "missing image" (e.g. next to a quiz). Only render when there's an actual image.
  if (!src || failed) return null;

  return (
    <figure id={id} className="book-figure">
      <img
        src={src}
        alt={caption || sourceRef || id}
        loading="lazy"
        onError={() => setFailed(true)}
      />
      {sourceRef ? (
        <figcaption>
          <SourceRef id={sourceRef} quote={caption} />
        </figcaption>
      ) : null}
    </figure>
  );
}
