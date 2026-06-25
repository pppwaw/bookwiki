import { defineConfig, defineDocs } from "fumadocs-mdx/config";
import { metaSchema, pageSchema } from "fumadocs-core/source/schema";
import { remarkMdxMermaid } from "fumadocs-core/mdx-plugins";
import remarkCjkFriendly from "remark-cjk-friendly";
import remarkMath from "remark-math";
import { z } from "zod";
import { rehypeMath } from "./lib/rehype-math";

const bookwikiPageSchema = pageSchema.extend({
  chapter_id: z.string().optional(),
  type: z.string().optional(),
  summary: z.string().optional(),
  // Chapter-only structured keypoints sourced from SummaryResult.key_points.
  // Fed to the Feynman learning panel as the "explain these" prompt.
  key_points: z.array(z.string()).optional(),
  concepts: z.array(z.string()).optional(),
  order_index: z.number().optional(),
});

const bookwikiMetaSchema = metaSchema.extend({
  root: z.boolean().optional(),
});

// You can customize Zod schemas for frontmatter and `meta.json` here
// see https://fumadocs.dev/docs/mdx/collections
export const docs = defineDocs({
  dir: "content/docs",
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
    providerImportSource: "@/components/mdx",
    remarkPlugins: [remarkCjkFriendly, remarkMath, remarkMdxMermaid],
    rehypePlugins: (v) => [rehypeMath, ...v],
  },
});
