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
  return (
    <figure id={id} className="book-figure">
      {src ? <img src={src} alt={caption || sourceRef} loading="lazy" /> : null}
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
