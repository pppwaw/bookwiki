import Link from 'next/link';
import type { ReactNode } from 'react';

export function ConceptLink({ slug, children }: { slug?: string; children: ReactNode }) {
  const target = slug ?? String(children);

  return <Link href={`/docs/concepts/${encodeURIComponent(target)}`}>{children}</Link>;
}
