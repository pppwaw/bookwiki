import Link from 'next/link';
import { getSourcePage, source } from '@/lib/source';
import { docsRoute } from '@/lib/shared';
import { Markdown } from '@/components/markdown';

type SourcePage = ReturnType<typeof source.getPages>[number];

const MAX_ORDER = Number.MAX_SAFE_INTEGER;

function byChapterOrder(a: SourcePage, b: SourcePage): number {
  return (a.data.order_index ?? MAX_ORDER) - (b.data.order_index ?? MAX_ORDER);
}

function byTitle(a: SourcePage, b: SourcePage): number {
  return a.data.title.localeCompare(b.data.title);
}

function ArrowRight({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M3.5 8h9M8.5 4l4 4-4 4" />
    </svg>
  );
}

export default function HomePage() {
  const page = getSourcePage(undefined);
  const title = page?.data.title?.trim() || 'BookWiki';
  const description =
    page?.data.description?.trim() ||
    '把一本书的原始材料，整理成可以检索、可以提问、可以逐章学习的知识库。';

  const pages = source.getPages();
  const chapters = pages.filter((p) => p.data.type === 'chapter').sort(byChapterOrder);
  const concepts = pages.filter((p) => p.data.type === 'concept').sort(byTitle);

  const monogram = Array.from(title)[0] ?? 'B';
  const startHref = chapters[0]?.url ?? docsRoute;

  return (
    <main className="bw-home">
      <section className="bw-cover">
        <div className="bw-cover-body">
          <p className="bw-kicker">
            <span className="bw-kicker-dot" aria-hidden="true" />
            交互式学习手册
          </p>
          <h1 className="bw-title">{title}</h1>
          <p className="bw-lead">{description}</p>
          <div className="bw-meta">
            <span className="bw-meta-item">
              <strong>{chapters.length}</strong>章正文
            </span>
            <span className="bw-meta-sep" aria-hidden="true" />
            <span className="bw-meta-item">
              <strong>{concepts.length}</strong>个核心概念
            </span>
          </div>
          <div className="bw-actions">
            <Link href={startHref} className="bw-btn bw-btn-primary">
              开始阅读
              <ArrowRight />
            </Link>
            {chapters.length > 0 && (
              <Link href="#toc" className="bw-btn bw-btn-ghost">
                浏览目录
              </Link>
            )}
          </div>
        </div>
        <div className="bw-plate" aria-hidden="true">
          <div className="bw-book">
            <span className="bw-book-spine" />
            <span className="bw-book-monogram">{monogram}</span>
          </div>
        </div>
      </section>

      {chapters.length > 0 && (
        <section className="bw-section" id="toc">
          <header className="bw-section-head">
            <h2 className="bw-section-title">目录</h2>
            <p className="bw-section-note">按顺序逐章展开</p>
          </header>
          <ol className="bw-toc">
            {chapters.map((chapter, index) => {
              const desc = chapter.data.summary ?? chapter.data.description;
              return (
                <li key={chapter.url} className="bw-toc-item">
                  <div className="bw-toc-link">
                    <span className="bw-toc-index" aria-hidden="true">
                      {String(index + 1).padStart(2, '0')}
                    </span>
                    <span className="bw-toc-text">
                      <Link href={chapter.url} className="bw-toc-name">
                        {chapter.data.title}
                      </Link>
                      {desc ? (
                        <span className="bw-toc-desc">
                          <Markdown text={desc} inline />
                        </span>
                      ) : null}
                    </span>
                    <ArrowRight className="bw-toc-arrow" />
                  </div>
                </li>
              );
            })}
          </ol>
        </section>
      )}

      {concepts.length > 0 && (
        <section className="bw-section">
          <header className="bw-section-head">
            <h2 className="bw-section-title">核心概念</h2>
            <p className="bw-section-note">点开任意概念，建立彼此的联系</p>
          </header>
          <ul className="bw-chips">
            {concepts.map((concept) => (
              <li key={concept.url}>
                <Link href={concept.url} className="bw-chip">
                  {concept.data.title}
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  );
}
