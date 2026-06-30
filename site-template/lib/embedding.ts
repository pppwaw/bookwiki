const DEFAULT_MODEL = 'baai/bge-m3';
const DEFAULT_BASE_URL = 'https://openrouter.ai/api/v1';

function normalize(vec: number[]): Float32Array {
  let norm = 0;
  for (const v of vec) norm += v * v;
  norm = Math.sqrt(norm) || 1;
  return Float32Array.from(vec, (v) => v / norm);
}

export async function embedQuery(query: string): Promise<Float32Array> {
  const apiKey = process.env.BOOKWIKI_CHAT_API_KEY;
  if (!apiKey) throw new Error('BOOKWIKI_CHAT_API_KEY 未配置,无法计算 query 向量');
  const baseUrl = process.env.BOOKWIKI_CHAT_BASE_URL ?? DEFAULT_BASE_URL;
  const model = process.env.BOOKWIKI_EMBED_MODEL ?? DEFAULT_MODEL;

  const resp = await fetch(`${baseUrl.replace(/\/$/, '')}/embeddings`, {
    method: 'POST',
    headers: { Authorization: `Bearer ${apiKey}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ model, input: query }),
  });
  if (!resp.ok) {
    const detail = await resp.text().catch(() => '');
    throw new Error(`embedding 请求失败 ${resp.status}: ${detail}`);
  }
  const body = (await resp.json()) as { data?: { embedding: number[] }[] };
  const embedding = body.data?.[0]?.embedding;
  if (!embedding) throw new Error('embedding 响应缺少向量');
  return normalize(embedding);
}
