'use client';

import './review.css';
import '@/components/highlight.css';

import { useMemo, useState } from 'react';
import Link from 'next/link';
import { MathText } from '@/components/MathText';
import {
  HighlightColors,
  removeHighlight,
  useHighlights,
  type Highlight,
  type HighlightColor,
} from '@/lib/highlights';

const COLOR_TITLES: Record<HighlightColor, string> = {
  amber: '黄',
  rose: '红',
  sky: '蓝',
  emerald: '绿',
};

type Group = { pagePath: string; pageTitle: string; items: Highlight[] };

function groupByPage(highlights: Highlight[]): Group[] {
  const map = new Map<string, Group>();
  for (const highlight of highlights) {
    const group = map.get(highlight.pagePath) ?? {
      pagePath: highlight.pagePath,
      pageTitle: highlight.pageTitle,
      items: [],
    };
    group.items.push(highlight);
    map.set(highlight.pagePath, group);
  }
  for (const group of map.values()) {
    group.items.sort((a, b) => a.createdAt - b.createdAt);
  }
  return [...map.values()].sort((a, b) => a.pageTitle.localeCompare(b.pageTitle, 'zh'));
}

export default function ReviewPage() {
  const { highlights, hydrated } = useHighlights();
  const [active, setActive] = useState<Set<HighlightColor>>(new Set(HighlightColors));

  const filtered = useMemo(
    () => highlights.filter((highlight) => active.has(highlight.color)),
    [highlights, active],
  );
  const groups = useMemo(() => groupByPage(filtered), [filtered]);

  const toggle = (color: HighlightColor) => {
    setActive((prev) => {
      const next = new Set(prev);
      if (next.has(color)) next.delete(color);
      else next.add(color);
      return next.size === 0 ? new Set(HighlightColors) : next;
    });
  };

  return (
    <main className="bookwiki-review">
      <header className="bookwiki-review-head">
        <h1>我的标记</h1>
        <p className="bookwiki-review-sub">
          {hydrated ? `共 ${highlights.length} 处标记` : '加载中…'}
        </p>
        <div className="bookwiki-review-filters">
          {HighlightColors.map((color) => (
            <button
              key={color}
              type="button"
              onClick={() => toggle(color)}
              className={`bookwiki-hl-chip bookwiki-hl-swatch-${color}${
                active.has(color) ? ' is-active' : ''
              }`}
            >
              {COLOR_TITLES[color]}
            </button>
          ))}
        </div>
      </header>

      {hydrated && groups.length === 0 ? (
        <p className="bookwiki-review-empty">
          还没有标记。在正文里选中文字即可划线,这里会汇总,考前集中过一遍。
        </p>
      ) : null}

      {groups.map((group) => (
        <section key={group.pagePath} className="bookwiki-review-group">
          <h2>
            <Link href={group.pagePath}>{group.pageTitle}</Link>
          </h2>
          <ul>
            {group.items.map((item) => (
              <li key={item.id} className={`bookwiki-review-item bookwiki-hl-border-${item.color}`}>
                <Link href={`${item.pagePath}?hl=${item.id}`} className="bookwiki-review-quote">
                  <MathText text={item.quoteRich ?? item.quote} />
                </Link>
                {item.note ? <p className="bookwiki-review-note">{item.note}</p> : null}
                <button
                  type="button"
                  className="bookwiki-review-remove"
                  onClick={() => removeHighlight(item.id)}
                >
                  删除
                </button>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </main>
  );
}
