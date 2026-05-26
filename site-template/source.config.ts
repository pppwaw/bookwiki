import { defineConfig, defineDocs } from 'fumadocs-mdx/config';
import { metaSchema, pageSchema } from 'fumadocs-core/source/schema';
import rehypeKatex from 'rehype-katex';
import remarkMath from 'remark-math';
import { z } from 'zod';

const bookwikiPageSchema = pageSchema.extend({
  chapter_id: z.string().optional(),
  type: z.string().optional(),
  summary: z.string().optional(),
  concepts: z.array(z.string()).optional(),
  order_index: z.number().optional(),
});

const bookwikiMetaSchema = metaSchema.extend({
  root: z.boolean().optional(),
});

// You can customize Zod schemas for frontmatter and `meta.json` here
// see https://fumadocs.dev/docs/mdx/collections
export const docs = defineDocs({
  dir: 'content/docs',
  docs: {
    schema: bookwikiPageSchema,
    postprocess: {
      includeProcessedMarkdown: true,
    },
  },
  meta: {
    schema: bookwikiMetaSchema,
  },
});

export default defineConfig({
  mdxOptions: {
    providerImportSource: '@/components/mdx',
    remarkPlugins: [remarkMath],
    rehypePlugins: (v) => [rehypeKatex, ...v],
  },
});
