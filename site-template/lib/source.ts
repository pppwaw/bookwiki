import { docs } from 'collections/server';
import { loader } from 'fumadocs-core/source';
import { lucideIconsPlugin } from 'fumadocs-core/source/lucide-icons';
import { docsContentRoute, docsImageRoute, docsRoute } from './shared';

// See https://fumadocs.dev/docs/headless/source-api for more info
export const source = loader({
  baseUrl: docsRoute,
  source: docs.toFumadocsSource(),
  plugins: [lucideIconsPlugin()],
});

type SourcePage = (typeof source)['$inferPage'];

export function getSourcePage(slug: string[] | undefined): SourcePage | undefined {
  const direct = source.getPage(slug);
  if (direct) return direct;

  const requested = slug ?? [];
  const decoded = requested.map(safeDecodeURIComponent);
  const decodedPage = source.getPage(decoded);
  if (decodedPage) return decodedPage;

  return source.getPages().find((page) => {
    if (page.slugs.length !== decoded.length) return false;

    return page.slugs.every((segment, index) =>
      slugSegmentMatches(segment, requested[index], decoded[index]),
    );
  });
}

function slugSegmentMatches(segment: string, raw: string, decoded: string): boolean {
  const decodedSegment = safeDecodeURIComponent(segment);

  return (
    segment === raw ||
    segment === decoded ||
    decodedSegment === raw ||
    decodedSegment === decoded ||
    encodeURIComponent(segment) === raw ||
    encodeURIComponent(decodedSegment) === raw ||
    encodeUtf8AsGbk(decodedSegment) === raw ||
    encodeUtf8AsGbk(decodedSegment) === decoded
  );
}

function safeDecodeURIComponent(value: string): string {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

function encodeUtf8AsGbk(value: string): string {
  try {
    return new TextDecoder('gbk').decode(new TextEncoder().encode(value));
  } catch {
    return value;
  }
}

export function getPageImage(page: (typeof source)['$inferPage']) {
  const segments = [...page.slugs, 'image.png'];

  return {
    segments,
    url: `${docsImageRoute}/${segments.join('/')}`,
  };
}

export function getPageMarkdownUrl(page: (typeof source)['$inferPage']) {
  const segments = [...page.slugs, 'content.md'];

  return {
    segments,
    url: `${docsContentRoute}/${segments.join('/')}`,
  };
}

export async function getLLMText(page: (typeof source)['$inferPage']) {
  const processed = await page.data.getText('processed');

  return `# ${page.data.title} (${page.url})

${processed}`;
}
