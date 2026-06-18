/**
 * Model context-window helpers for the chat route.
 *
 * The chat injects the current article into the prompt for grounding. To avoid
 * overflowing the model's context window (which causes the provider to error or
 * silently drop content), the article is trimmed to a token budget derived from
 * the model's advertised `context_length`. The window is looked up from the
 * OpenRouter `/models` endpoint (cached) and falls back to an env-configurable
 * value when the lookup fails.
 */

type CacheEntry = {
  tokens: number;
  expires: number;
};

const contextCache = new Map<string, CacheEntry>();
const CacheTtlMs = 60 * 60 * 1000;
const LookupTimeoutMs = 5000;

/**
 * Resolve the model's context window (in tokens) from OpenRouter, cached for an
 * hour. Returns null when the model is unknown or the request fails, so callers
 * can fall back to a configured default.
 */
export async function modelContextTokens(
  model: string,
  apiKey: string,
  baseURL: string,
): Promise<number | null> {
  const cached = contextCache.get(model);
  if (cached && cached.expires > Date.now()) return cached.tokens;

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), LookupTimeoutMs);

  try {
    const endpoint = `${baseURL.replace(/\/+$/, '')}/models`;
    const response = await fetch(endpoint, {
      headers: { Authorization: `Bearer ${apiKey}` },
      signal: controller.signal,
    });
    if (!response.ok) return null;

    const payload = (await response.json()) as {
      data?: Array<{ id?: string; context_length?: number }>;
    };
    const entry = payload.data?.find((item) => item.id === model);
    const tokens =
      typeof entry?.context_length === 'number' && entry.context_length > 0
        ? entry.context_length
        : null;

    if (tokens) contextCache.set(model, { tokens, expires: Date.now() + CacheTtlMs });
    return tokens;
  } catch {
    return null;
  } finally {
    clearTimeout(timeout);
  }
}

/**
 * Compute the article token budget from the full context window, reserving room
 * for the system prompt, conversation history, tool results, and the response.
 */
export function articleTokenBudget(contextTokens: number, outputTokens: number): number {
  const overhead = outputTokens + 3000;
  const remaining = contextTokens - overhead;
  // Leave at least 40% of the window for conversation history and tool results.
  const capped = Math.min(remaining, Math.floor(contextTokens * 0.6));
  return Math.max(capped, 1000);
}

/** Rough token estimate: CJK glyphs ~1 token each, other text ~4 chars/token. */
export function estimateTokens(text: string): number {
  let cjk = 0;
  let other = 0;
  for (const char of text) {
    if (isCjk(char.codePointAt(0) ?? 0)) cjk += 1;
    else other += 1;
  }
  return Math.ceil(cjk + other / 4);
}

/** Trim text so its estimated token count stays within `maxTokens`. */
export function truncateToTokenBudget(text: string, maxTokens: number): string {
  if (maxTokens <= 0) return '';
  if (estimateTokens(text) <= maxTokens) return text;

  const chars = Array.from(text);
  let tokens = 0;
  let count = 0;
  for (const char of chars) {
    tokens += isCjk(char.codePointAt(0) ?? 0) ? 1 : 0.25;
    if (tokens > maxTokens) break;
    count += 1;
  }

  return chars.slice(0, count).join('');
}

/** Parse a positive integer env var, returning null when unset or invalid. */
export function positiveIntFromEnv(name: string): number | null {
  const raw = process.env[name];
  if (!raw) return null;
  const value = Number.parseInt(raw, 10);
  return Number.isFinite(value) && value > 0 ? value : null;
}

function isCjk(code: number): boolean {
  return (
    (code >= 0x3000 && code <= 0x303f) || // CJK symbols and punctuation
    (code >= 0x3040 && code <= 0x30ff) || // Hiragana + Katakana
    (code >= 0x3400 && code <= 0x4dbf) || // CJK Extension A
    (code >= 0x4e00 && code <= 0x9fff) || // CJK Unified Ideographs
    (code >= 0xac00 && code <= 0xd7af) || // Hangul syllables
    (code >= 0xf900 && code <= 0xfaff) || // CJK compatibility ideographs
    (code >= 0xff00 && code <= 0xffef) // Halfwidth + fullwidth forms
  );
}
