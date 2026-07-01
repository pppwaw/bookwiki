import { expect, test } from 'vitest';
import { citationGroupRegex, tokensFromMatch } from './citations';

/** Parse the first citation group found in `text` (or null when none matches). */
function parse(text: string) {
  for (const match of text.matchAll(citationGroupRegex())) {
    return tokensFromMatch(match);
  }
  return undefined;
}

test('recognizes a Chinese page slug and strips the docs/ prefix', () => {
  expect(parse('[^docs/chapters/感知器-Perceptron]')).toEqual([
    { kind: 'page', slug: 'chapters/感知器-Perceptron' },
  ]);
});

test('parses several comma-separated Chinese page citations in one bracket', () => {
  expect(parse('[^docs/chapters/感知器-Perceptron, ^docs/concepts/感知器]')).toEqual([
    { kind: 'page', slug: 'chapters/感知器-Perceptron' },
    { kind: 'page', slug: 'concepts/感知器' },
  ]);
});

test('still recognizes a percent-encoded page slug', () => {
  const encoded = 'concepts/感知器'.split('/').map(encodeURIComponent).join('/');
  expect(parse(`[^${encoded}]`)).toEqual([{ kind: 'page', slug: encoded }]);
});

test('treats a Chinese source_ref (no slash) as a source token', () => {
  expect(parse('[^反向传播-p003]')).toEqual([{ kind: 'source', ref: '反向传播-p003' }]);
});

test('leaves ambiguous prose brackets as literal text', () => {
  // A caret bracket with an internal space is not a citation.
  expect(parse('[^note this]')).toBeNull();
});

test('drops a bare docs/ payload rather than emitting an empty slug', () => {
  expect(parse('[^docs/]')).toBeNull();
});
