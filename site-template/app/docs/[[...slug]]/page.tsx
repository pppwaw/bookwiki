import { getPageImage, getPageMarkdownUrl, getSourcePage, source } from '@/lib/source';
import {
  DocsBody,
  DocsDescription,
  DocsPage,
  DocsTitle,
  MarkdownCopyButton,
  ViewOptionsPopover,
} from 'fumadocs-ui/layouts/docs/page';
import { notFound, redirect } from 'next/navigation';
import { getMDXComponents } from '@/components/mdx';
import { ChapterSummary } from '@/components/ChapterSummary';
import { FeynmanPanel } from '@/components/FeynmanPanel';
import { Markdown } from '@/components/markdown';
import type { Metadata } from 'next';
import { createRelativeLink } from 'fumadocs-ui/mdx';
import { gitConfig } from '@/lib/shared';

const MAX_ORDER = Number.MAX_SAFE_INTEGER;

function firstChapterHref(): string | undefined {
  const chapters = source
    .getPages()
    .filter((page) => page.data.type === 'chapter')
    .sort((a, b) => (a.data.order_index ?? MAX_ORDER) - (b.data.order_index ?? MAX_ORDER));
  return chapters[0]?.url;
}

export default async function Page(props: PageProps<'/docs/[[...slug]]'>) {
  const params = await props.params;
  if (!params.slug?.length) {
    const firstChapter = firstChapterHref();
    if (firstChapter) redirect(firstChapter);
  }
  const page = getSourcePage(params.slug);
  if (!page) notFound();

  const MDX = page.data.body;
  const markdownUrl = getPageMarkdownUrl(page).url;
  const summary = (page.data as { summary?: string }).summary;
  const keyPoints = (page.data as { key_points?: string[] }).key_points ?? [];
  const pageType = page.data.type;

  return (
    <DocsPage toc={page.data.toc} full={page.data.full}>
      <DocsTitle>{page.data.title}</DocsTitle>
      <DocsDescription className="mb-0">{page.data.description}</DocsDescription>
      <div className="flex flex-row gap-2 items-center border-b pb-6">
        <MarkdownCopyButton markdownUrl={markdownUrl} />
        <ViewOptionsPopover
          markdownUrl={markdownUrl}
          githubUrl={`https://github.com/${gitConfig.user}/${gitConfig.repo}/blob/${gitConfig.branch}/content/docs/${page.path}`}
        />
      </div>
      <DocsBody>
        {summary ? <ChapterSummary><Markdown text={summary} /></ChapterSummary> : null}
        <MDX
          components={getMDXComponents({
            // this allows you to link to other pages with relative file paths
            a: createRelativeLink(source, page),
          })}
        />
        {pageType === 'chapter' || pageType === 'concept' ? (
          <FeynmanPanel
            scope={
              pageType === 'chapter'
                ? `第 ${(page.data as { order_index?: number }).order_index ?? '?'} 章 ${page.data.title}`
                : `概念:${page.data.title}`
            }
            keypoints={keyPoints}
            summary={summary}
          />
        ) : null}
      </DocsBody>
    </DocsPage>
  );
}

export async function generateStaticParams() {
  return source.generateParams();
}

export async function generateMetadata(props: PageProps<'/docs/[[...slug]]'>): Promise<Metadata> {
  const params = await props.params;
  const page = getSourcePage(params.slug);
  if (!page) notFound();

  return {
    title: page.data.title,
    description: page.data.description,
    openGraph: {
      images: getPageImage(page).url,
    },
  };
}
