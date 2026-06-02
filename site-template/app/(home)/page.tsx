import { getMDXComponents } from '@/components/mdx';
import { getSourcePage, source } from '@/lib/source';
import { createRelativeLink } from 'fumadocs-ui/mdx';
import { notFound } from 'next/navigation';

export default function HomePage() {
  const page = getSourcePage(undefined);
  if (!page) notFound();

  const MDX = page.data.body;

  return (
    <main className="mx-auto flex w-full max-w-5xl flex-1 flex-col px-6 py-10 sm:px-8 lg:px-10">
      <article className="prose min-w-0 max-w-none dark:prose-invert">
        <MDX
          components={getMDXComponents({
            a: createRelativeLink(source, page),
          })}
        />
      </article>
    </main>
  );
}
