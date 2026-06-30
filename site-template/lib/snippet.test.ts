import { expect, test } from 'vitest';
import { toPlainSnippet } from './snippet';

test('strips markdown headings and inline math', () => {
  const out = toPlainSnippet('## 用格林定理计算线积分\n\n其中 $C$ 的定向取正向。');
  expect(out).not.toContain('#');
  expect(out).not.toContain('$');
  expect(out).toContain('用格林定理计算线积分');
  expect(out).toContain('的定向取正向');
});

test('strips block math', () => {
  const out = toPlainSnippet('设 $$\\iint_S \\frac{\\partial N}{\\partial x} dA$$ 成立。');
  expect(out).not.toContain('\\iint');
  expect(out).toContain('成立');
});

test('strips MDX component, keeps inner text', () => {
  const raw =
    '## Referenced By\n\n- <PreviewLink href={"/docs/x"} summary={"长摘要..."}>14.4 Green\'s Theorem</PreviewLink>';
  const out = toPlainSnippet(raw);
  expect(out).not.toContain('PreviewLink');
  expect(out).not.toContain('summary');
  expect(out).not.toContain('href');
  expect(out).toContain('Referenced By');
  expect(out).toContain("Green's Theorem");
});

test('drops self-closing component with attribute blob', () => {
  const out = toPlainSnippet('<SourceRef id={"textbook-p001"} /> 正文内容在此。');
  expect(out).not.toContain('SourceRef');
  expect(out).not.toContain('textbook');
  expect(out).toContain('正文内容在此');
});

test('truncates long text with ellipsis', () => {
  const out = toPlainSnippet('字'.repeat(300), 50);
  expect(out.length).toBeLessThanOrEqual(51);
  expect(out.endsWith('…')).toBe(true);
});
