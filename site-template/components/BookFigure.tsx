import { MathText } from './MathText';
import { SourceRef } from './SourceRef';

export function BookFigure({
  id,
  src,
  sourceRef,
  caption,
}: {
  id: string;
  src?: string;
  sourceRef: string;
  caption?: string;
}) {
  // A figure with no image asset is just a caption-only box that reads as a phantom
  // "missing image" (e.g. next to a quiz). Only render when there's an actual image.
  if (!src) return null;
  return (
    <figure id={id} className="book-figure">
      <img src={src} alt={caption || sourceRef} loading="lazy" />
      {caption || sourceRef ? (
        <figcaption>
          {caption ? (
            <span>
              <MathText text={caption} />
            </span>
          ) : null}
          <SourceRef id={sourceRef} quote={caption} />
        </figcaption>
      ) : null}
    </figure>
  );
}
