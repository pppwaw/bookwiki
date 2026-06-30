import { afterEach, expect, test, vi } from 'vitest';

vi.mock('@/lib/rag', () => ({
  searchChunks: vi.fn(async () => [
    {
      chunkId: 'c1',
      pageId: 'p1',
      chapterId: 'ch01',
      title: '反向传播',
      slug: 'ch01',
      headingPath: '反向传播 > 概述',
      text: '反向传播用于训练神经网络',
      sourceRefs: [],
    },
  ]),
}));

import { GET } from './route';

afterEach(() => vi.clearAllMocks());

test('GET returns SortedResult array with page and text entries', async () => {
  const res = await GET(new Request('http://x/api/search?query=反向传播'));
  const data = (await res.json()) as { id: string; type: string; url: string; content: string }[];
  expect(Array.isArray(data)).toBe(true);
  expect(data.some((d) => d.type === 'page')).toBe(true);
  expect(data.some((d) => d.type === 'text')).toBe(true);
  expect(data.every((d) => typeof d.url === 'string')).toBe(true);
});

test('GET empty query returns empty array', async () => {
  const res = await GET(new Request('http://x/api/search?query='));
  expect(await res.json()).toEqual([]);
});
