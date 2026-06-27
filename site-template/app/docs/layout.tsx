import { Suspense } from 'react';
import { source } from '@/lib/source';
import { DocsLayout } from 'fumadocs-ui/layouts/docs';
import { baseOptions } from '@/lib/layout.shared';
import { KatexClient } from '@/components/KatexClient';
import { HighlightLayer, DOC_ROOT_ID } from '@/components/HighlightLayer';
import { HighlightToolbar } from '@/components/HighlightToolbar';

export default function Layout({ children }: LayoutProps<'/docs'>) {
  return (
    <DocsLayout tree={source.getPageTree()} {...baseOptions()}>
      <KatexClient />
      <div id={DOC_ROOT_ID} style={{ display: 'contents' }}>
        {children}
      </div>
      <Suspense fallback={null}>
        <HighlightLayer />
      </Suspense>
      <HighlightToolbar />
    </DocsLayout>
  );
}
