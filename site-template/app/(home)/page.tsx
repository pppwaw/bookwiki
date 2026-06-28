import './home.css';

import Link from 'next/link';
import { getSourcePage, source } from '@/lib/source';
import { docsRoute } from '@/lib/shared';
import { hasAnyCards } from '@/lib/anki';
import { Markdown } from '@/components/markdown';
import { ConceptGraph } from '@/components/concept-graph';

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

// Colophon / publisher's device for the book cover: a sunrise over a horizon
// inside a seal — an editorial "dawn of understanding" mark that complements the
// cover without duplicating the book metaphor. Purely decorative.
function CoverMark({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 64 64"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <circle cx="32" cy="32" r="22" strokeWidth="1.5" />
      <path d="M15 41h34" />
      <path d="M24 41a8 8 0 0 1 16 0" />
      <path d="M32 28v-4M25 30l-3-3M39 30l3-3" strokeWidth="1.6" />
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

  const startHref = chapters[0]?.url ?? docsRoute;
  // Plain anchor (not <Link>): /api/anki returns a CSV download, not a page.
  // Href kept as a variable so next/no-html-link-for-pages does not flag it.
  const ankiExportHref = '/api/anki';
  const showAnkiExport = hasAnyCards();

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
            {showAnkiExport && (
              <a href={ankiExportHref} className="bw-btn bw-btn-ghost">
                ⬇ 导出全书 Anki 卡片
              </a>
            )}
          </div>
        </div>
        <div className="bw-plate" aria-hidden="true">
          <div className="bw-book">
            <span className="bw-book-spine" />
            <div className="bw-book-face">
              <div className="bw-book-plate">
                <span className="bw-book-line" />
                <span className="bw-book-line" />
                <span className="bw-book-byline" />
              </div>
              <CoverMark className="bw-book-mark" />
            </div>
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
            <p className="bw-section-note">在关系图中探索概念之间的联系，点击任意节点进入</p>
          </header>
          <ConceptGraph />
        </section>
      )}
    </main>
  );
}
