import { afterEach, expect, test, vi } from 'vitest';
import { embedQuery } from './embedding';

afterEach(() => vi.unstubAllGlobals());

test('embedQuery posts and returns normalized vector', async () => {
  process.env.BOOKWIKI_CHAT_API_KEY = 'k';
  const fetchMock = vi.fn(async () => ({
    ok: true,
    json: async () => ({ data: [{ embedding: [3, 4] }] }),
  }));
  vi.stubGlobal('fetch', fetchMock);
  const vec = await embedQuery('反向传播');
  expect(Math.hypot(vec[0], vec[1])).toBeCloseTo(1, 5);
  expect(fetchMock).toHaveBeenCalledOnce();
});

test('embedQuery throws on non-ok', async () => {
  process.env.BOOKWIKI_CHAT_API_KEY = 'k';
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({ ok: false, status: 500, text: async () => 'boom' })),
  );
  await expect(embedQuery('x')).rejects.toThrow();
});
