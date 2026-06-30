import { expect, test } from 'vitest';
import { blobToFloat32, dot, topNByDot } from './vector';

test('blobToFloat32 reads little-endian float32', () => {
  const src = new Float32Array([1, 2, 3]);
  const buf = Buffer.from(src.buffer);
  expect(Array.from(blobToFloat32(buf))).toEqual([1, 2, 3]);
});

test('dot computes inner product', () => {
  expect(dot(new Float32Array([1, 0, 1]), new Float32Array([1, 2, 3]))).toBe(4);
});

test('topNByDot ranks by score desc', () => {
  const q = new Float32Array([1, 0]);
  const items = [
    { id: 'a', vec: new Float32Array([0, 1]) },
    { id: 'b', vec: new Float32Array([1, 0]) },
  ];
  const out = topNByDot(q, items, 1);
  expect(out[0].id).toBe('b');
});
