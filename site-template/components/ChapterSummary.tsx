import type { ReactNode } from 'react';

export function ChapterSummary({ children }: { children: ReactNode }) {
  return (
    <aside className="chapter-summary" aria-label="Chapter summary">
      <div className="chapter-summary-tag">Summary</div>
      <div className="chapter-summary-body">{children}</div>
    </aside>
  );
}
