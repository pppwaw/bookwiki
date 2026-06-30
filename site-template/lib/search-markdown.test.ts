import { expect, test } from 'vitest';
import { markdownToHast } from './search-markdown';

test('inline math renders to KaTeX nodes', () => {
  const tree = JSON.stringify(markdownToHast('质量能量 $E = mc^2$ 守恒'));
  expect(tree).toContain('katex'); // rehype-katex produced math markup
  expect(tree).toContain('守恒'); // surrounding prose kept
});

test('block math renders to KaTeX', () => {
  const tree = JSON.stringify(markdownToHast('$$\\iint_S \\frac{\\partial N}{\\partial x} dA$$'));
  expect(tree).toContain('katex');
});

test('plain prose passes through', () => {
  const tree = JSON.stringify(markdownToHast('普通段落文本。'));
  expect(tree).toContain('普通段落文本');
  expect(tree).not.toContain('katex');
});
