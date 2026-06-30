export function floatsBlob(vec: number[]): Buffer {
  return Buffer.from(Float32Array.from(vec).buffer);
}
