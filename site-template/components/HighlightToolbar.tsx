'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import { usePathname } from 'next/navigation';
import {
  HighlightColors,
  addHighlight,
  getHighlights,
  removeHighlight,
  updateHighlight,
  type HighlightColor,
  type NewHighlight,
} from '@/lib/highlights';
import { anchorContainsOffset, anchorFromRange, type TextAnchor } from '@/lib/highlight-anchor';
import { DOC_ROOT_ID } from './HighlightLayer';

const COLOR_TITLES: Record<HighlightColor, string> = {
  amber: '黄',
  rose: '红',
  sky: '蓝',
  emerald: '绿',
};

type Pos = { top: number; left: number };

type Popover =
  | { kind: 'create'; pos: Pos; anchor: TextAnchor }
  | { kind: 'edit'; pos: Pos; id: string }
  | null;

function caretOffset(root: HTMLElement, x: number, y: number): number | null {
  const doc = document as Document & {
    caretPositionFromPoint?: (x: number, y: number) => { offsetNode: Node; offset: number } | null;
    caretRangeFromPoint?: (x: number, y: number) => Range | null;
  };
  let node: Node | null = null;
  let offset = 0;
  if (typeof doc.caretPositionFromPoint === 'function') {
    const pos = doc.caretPositionFromPoint(x, y);
    if (!pos) return null;
    node = pos.offsetNode;
    offset = pos.offset;
  } else if (typeof doc.caretRangeFromPoint === 'function') {
    const range = doc.caretRangeFromPoint(x, y);
    if (!range) return null;
    node = range.startContainer;
    offset = range.startOffset;
  } else {
    return null;
  }
  if (!node || !root.contains(node)) return null;
  const pre = document.createRange();
  pre.selectNodeContents(root);
  pre.setEnd(node, offset);
  return pre.toString().length;
}

function clampPos(top: number, left: number): Pos {
  if (typeof window === 'undefined') return { top, left };
  return {
    top: Math.max(8, top),
    left: Math.min(Math.max(8, left), window.innerWidth - 220),
  };
}

export function HighlightToolbar() {
  const pathname = usePathname();
  const [popover, setPopover] = useState<Popover>(null);
  const [note, setNote] = useState('');
  const ref = useRef<HTMLDivElement>(null);

  const close = useCallback(() => {
    setPopover(null);
    setNote('');
  }, []);

  const pageContext = useCallback((): { pagePath: string; pageTitle: string } => {
    const root = document.getElementById(DOC_ROOT_ID);
    const title = root?.querySelector('h1')?.textContent?.trim();
    return { pagePath: pathname, pageTitle: title || document.title || pathname };
  }, [pathname]);

  useEffect(() => {
    const onPointerUp = (event: PointerEvent | MouseEvent) => {
      if (ref.current?.contains(event.target as Node)) return; // clicks inside the popover
      const root = document.getElementById(DOC_ROOT_ID);
      if (!root) return;

      const selection = window.getSelection();
      const text = selection?.toString() ?? '';

      if (selection && !selection.isCollapsed && text.trim()) {
        const range = selection.getRangeAt(0);
        if (!root.contains(range.commonAncestorContainer)) {
          close();
          return;
        }
        const anchor = anchorFromRange(root, range);
        if (!anchor) {
          close();
          return;
        }
        const rect = range.getBoundingClientRect();
        setNote('');
        setPopover({ kind: 'create', anchor, pos: clampPos(rect.top - 52, rect.left) });
        return;
      }

      // Collapsed click: open the editor if it landed on an existing highlight.
      const offset = caretOffset(root, event.clientX, event.clientY);
      if (offset === null) {
        close();
        return;
      }
      const haystack = root.textContent ?? '';
      const hit = getHighlights().find(
        (highlight) =>
          highlight.pagePath === pathname && anchorContainsOffset(haystack, highlight, offset),
      );
      if (hit) {
        setNote(hit.note ?? '');
        setPopover({ kind: 'edit', id: hit.id, pos: clampPos(event.clientY + 12, event.clientX) });
      } else {
        close();
      }
    };

    document.addEventListener('pointerup', onPointerUp);
    return () => document.removeEventListener('pointerup', onPointerUp);
  }, [pathname, close]);

  useEffect(() => {
    close();
  }, [pathname, close]);

  const onPickCreate = (color: HighlightColor) => {
    if (popover?.kind !== 'create') return;
    const { pagePath, pageTitle } = pageContext();
    const input: NewHighlight = { ...popover.anchor, pagePath, pageTitle, color };
    if (note.trim()) input.note = note.trim();
    addHighlight(input);
    window.getSelection()?.removeAllRanges();
    close();
  };

  const onPickEdit = (color: HighlightColor) => {
    if (popover?.kind !== 'edit') return;
    updateHighlight(popover.id, { color });
  };

  const onSaveNote = () => {
    if (popover?.kind !== 'edit') return;
    updateHighlight(popover.id, { note: note.trim() || undefined });
    close();
  };

  const onDelete = () => {
    if (popover?.kind !== 'edit') return;
    removeHighlight(popover.id);
    close();
  };

  if (!popover) return null;

  return (
    <div
      ref={ref}
      className="bookwiki-hl-toolbar"
      style={{ top: popover.pos.top, left: popover.pos.left }}
      role="dialog"
      aria-label="标记"
    >
      <div className="bookwiki-hl-swatches">
        {HighlightColors.map((color) => (
          <button
            key={color}
            type="button"
            title={COLOR_TITLES[color]}
            aria-label={COLOR_TITLES[color]}
            className={`bookwiki-hl-swatch bookwiki-hl-swatch-${color}`}
            onClick={() => (popover.kind === 'create' ? onPickCreate(color) : onPickEdit(color))}
          />
        ))}
        {popover.kind === 'edit' ? (
          <button type="button" className="bookwiki-hl-del" onClick={onDelete} title="删除">
            ✕
          </button>
        ) : null}
      </div>
      <input
        className="bookwiki-hl-note"
        value={note}
        placeholder="批注(可选)"
        onChange={(event) => setNote(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && popover.kind === 'edit') onSaveNote();
          if (event.key === 'Escape') close();
        }}
        onBlur={() => {
          if (popover.kind === 'edit') onSaveNote();
        }}
      />
    </div>
  );
}
