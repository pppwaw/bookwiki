import { expect, test } from 'vitest';
import { decodeSlug, safeDecodeURIComponent } from './slug';

test('safeDecodeURIComponent decodes percent-encoded Chinese', () => {
  expect(safeDecodeURIComponent('%E6%84%9F%E5%BA%94')).toBe('感应');
});

test('safeDecodeURIComponent leaves malformed input untouched', () => {
  // A lone `%` is not valid percent-encoding and must not throw.
  expect(safeDecodeURIComponent('100%')).toBe('100%');
});

test('safeDecodeURIComponent is a no-op on already-decoded text', () => {
  expect(safeDecodeURIComponent('感应')).toBe('感应');
});

test('decodeSlug decodes each segment and keeps separators', () => {
  const encoded = '概念/感应'.split('/').map(encodeURIComponent).join('/');
  expect(decodeSlug(encoded)).toBe('概念/感应');
});
