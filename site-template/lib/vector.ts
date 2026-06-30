export function blobToFloat32(buf: Buffer | Uint8Array): Float32Array {
  const bytes = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  if (bytes.byteOffset % 4 === 0) {
    return new Float32Array(bytes.buffer, bytes.byteOffset, Math.floor(bytes.byteLength / 4));
  }
  return new Float32Array(bytes.slice().buffer);
}

export function dot(a: Float32Array, b: Float32Array): number {
  let sum = 0;
  const len = Math.min(a.length, b.length);
  for (let i = 0; i < len; i += 1) sum += a[i] * b[i];
  return sum;
}

export function topNByDot(
  query: Float32Array,
  items: { id: string; vec: Float32Array }[],
  n: number,
): { id: string; score: number }[] {
  return items
    .map((item) => ({ id: item.id, score: dot(query, item.vec) }))
    .sort((a, b) => b.score - a.score)
    .slice(0, n);
}
