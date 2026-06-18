import Link from 'next/link';
import type { ReactNode } from 'react';
import { MathText } from './MathText';

export function PreviewLink({
  href,
  title,
  summary,
  children,
}: {
  href: string;
  title?: string;
  summary?: string;
  children: ReactNode;
}) {
  const hasPreview = Boolean(title || summary);

  return (
    <span className="preview-link-wrap">
      <Link className="preview-link" href={href}>
        {children}
      </Link>
      {hasPreview ? (
        <span className="preview-link-card" role="tooltip">
          {title ? (
            <span className="preview-link-title">
              <MathText text={title} />
            </span>
          ) : null}
          {summary ? (
            <span className="preview-link-summary">
              <MathText text={summary} />
            </span>
          ) : null}
        </span>
      ) : null}
    </span>
  );
}
