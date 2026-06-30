import { expect, test } from 'vitest';
import { rrf } from './fusion';

test('rrf ranks the consistently-high item first', () => {
  const kw = ['a', 'b', 'c', 'd'];
  const vec = ['a', 'c', 'b', 'd'];
  const out = rrf([kw, vec]);
  expect(out[0].id).toBe('a'); // 两表都第 1
});

test('rrf includes items present in only one list', () => {
  const out = rrf([['x'], ['y']]);
  const ids = out.map((r) => r.id);
  expect(ids).toEqual(expect.arrayContaining(['x', 'y']));
});
