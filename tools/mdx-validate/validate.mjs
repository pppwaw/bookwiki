#!/usr/bin/env node
// Validate MDX with the SAME remark config as the fumadocs site
// (remark-cjk-friendly + remark-math), so generation-time validation matches what the
// site's MDX parser will accept AND prerender.
//
// Two layers:
//   1. compile() — catches anything that breaks the MDX *parse/compile* (bare `<`/`>`
//      comparisons, unclosed tags, `{...}` that is not valid JS, ...).
//   2. bare-expression scan — catches `{X}` style JSX flow/text expressions that compile
//      fine but throw `ReferenceError: X is not defined` at *prerender* (e.g. an inline
//      `<cite>` wrapping bare LaTeX `\bar{X}`). In BookWiki content every brace is either
//      LaTeX (inside `$...$`, consumed by remark-math) or a component attribute, so any
//      `mdxTextExpression`/`mdxFlowExpression` node is a build-breaking bug.
//
// Reads MDX from stdin, prints a JSON line: {"ok": bool, "errors": [{message,line,column}]}.
// A non-zero exit code is reserved for internal failures (e.g. the toolchain itself broke),
// NOT for invalid MDX — invalid MDX is a normal result reported via {"ok": false, ...}.
import { compile } from "@mdx-js/mdx";
import katex from "katex";
import remarkCjkFriendly from "remark-cjk-friendly";
import remarkMath from "remark-math";
import remarkMdx from "remark-mdx";
import remarkParse from "remark-parse";
import { unified } from "unified";
import { visit } from "unist-util-visit";

function readStdin() {
  return new Promise((resolve, reject) => {
    let input = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      input += chunk;
    });
    process.stdin.on("end", () => resolve(input));
    process.stdin.on("error", reject);
  });
}

function diagnostic(err) {
  const place = err && err.place ? err.place : null;
  const start = place && place.start ? place.start : place;
  return {
    message: String((err && (err.reason || err.message)) || err),
    line: (err && err.line) ?? (start && start.line) ?? null,
    column: (err && err.column) ?? (start && start.column) ?? null,
    rule: (err && err.ruleId) || null,
  };
}

// Same remark stack as the site, used only to walk the tree. remark-math is essential:
// it consumes `$...$`/`$$...$$` into math nodes so LaTeX braces are NOT seen as JSX
// expressions; only braces in real flow/text position survive as mdx expression nodes.
const exprProcessor = unified()
  .use(remarkParse)
  .use(remarkMdx)
  .use(remarkCjkFriendly)
  .use(remarkMath);

function findBareExpressions(tree) {
  const errors = [];
  visit(tree, (node) => {
    if (node.type !== "mdxTextExpression" && node.type !== "mdxFlowExpression") {
      return;
    }
    const start = (node.position && node.position.start) || {};
    const value = String(node.value || "").replace(/\s+/g, " ").slice(0, 40);
    errors.push({
      message:
        `bare JSX expression {${value}} compiles but renders as JS and crashes prerender ` +
        "(ReferenceError); wrap math in $...$ or remove the inline tag",
      line: start.line ?? null,
      column: start.column ?? null,
      rule: "no-bare-expression",
    });
  });
  return errors;
}

// Render-time-unsafe JSX that still *compiles* and so slips past both compile() and the
// bare-expression scan. The motivating case: `<cite ref="p001">` compiles as MDX but
// crashes Next prerender with "Refs cannot be used in Server Components" because React
// treats `ref` as a reserved field. These rules reject the dangerous shapes at
// generation time instead of letting them reach the site build.
const DANGEROUS_ELEMENTS = new Set([
  "script",
  "iframe",
  "object",
  "embed",
  "style",
  "link",
  "meta",
  "base",
]);
const UNSAFE_PROPS = new Set(["ref", "dangerouslySetInnerHTML"]);

function isEventProp(name) {
  // React DOM event handlers: onClick, onError, onMouseOver, ... (on + UpperCase).
  return /^on[A-Z]/.test(name);
}

function findDisallowedJsx(tree) {
  const errors = [];
  visit(tree, (node) => {
    if (node.type !== "mdxJsxFlowElement" && node.type !== "mdxJsxTextElement") {
      return;
    }
    const name = node.name || "";
    const start = (node.position && node.position.start) || {};
    if (name && DANGEROUS_ELEMENTS.has(name)) {
      errors.push({
        message:
          `disallowed raw HTML element <${name}>: it does not render safely in the site; ` +
          "remove it",
        line: start.line ?? null,
        column: start.column ?? null,
        rule: "no-raw-html-element",
      });
    }
    for (const attr of node.attributes || []) {
      if (attr.type !== "mdxJsxAttribute" || typeof attr.name !== "string") {
        continue;
      }
      if (UNSAFE_PROPS.has(attr.name) || isEventProp(attr.name)) {
        errors.push({
          message:
            `disallowed prop "${attr.name}" on <${name || "fragment"}>: it is a ` +
            "reserved/unsafe React prop that breaks Server Component prerender; " +
            "use <SourceRef ... /> for citations",
          line: start.line ?? null,
          column: start.column ?? null,
          rule: "no-unsafe-jsx-prop",
        });
      }
    }
  });
  return errors;
}

// Mirror of the site's `normalizeKatexInput` (site-template/lib/katex.ts): the page
// rewrites circled digits and θ-in-\text before KaTeX runs, so validation MUST apply the
// same rewrite or it would flag math that actually renders fine on the site.
const KATEX_TEXT_MODE_DIGITS = {
  "①": "1",
  "②": "2",
  "③": "3",
  "④": "4",
  "⑤": "5",
  "⑥": "6",
  "⑦": "7",
  "⑧": "8",
  "⑨": "9",
  "⑩": "10",
};

function normalizeKatexInput(tex) {
  return tex
    .replace(/[①②③④⑤⑥⑦⑧⑨⑩]/g, (value) => KATEX_TEXT_MODE_DIGITS[value] ?? value)
    .replace(/\\text\{([^{}]*)\}/g, (_match, text) => {
      if (!text.includes("θ")) return `\\text{${text}}`;
      return text
        .split("θ")
        .map((part) => (part ? `\\text{${part}}` : ""))
        .join("\\theta");
    });
}

// remark-math parses `$...$`/`$$...$$` into math nodes but does NOT interpret the TeX,
// so invalid LaTeX (undefined control sequences, mismatched braces) compiles cleanly and
// then renders as raw text on the site, because the client KaTeX runs with
// `throwOnError: false`. Re-render every math node here with `throwOnError: true` (same
// `strict: false` + normalization as the site) so broken math is caught at validation
// time instead of silently degrading on the page.
function findBrokenMath(tree) {
  const errors = [];
  visit(tree, (node) => {
    if (node.type !== "math" && node.type !== "inlineMath") {
      return;
    }
    const start = (node.position && node.position.start) || {};
    const tex = String(node.value || "");
    try {
      katex.renderToString(normalizeKatexInput(tex), {
        throwOnError: true,
        strict: false,
        output: "html",
        displayMode: node.type === "math",
      });
    } catch (err) {
      const reason = String((err && (err.message || err.reason)) || err)
        .replace(/\s+/g, " ")
        .slice(0, 160);
      const snippet = tex.replace(/\s+/g, " ").slice(0, 40);
      errors.push({
        message:
          `invalid LaTeX math ($${snippet}$): ${reason}; it renders as raw text on the ` +
          "site (KaTeX throwOnError is off) — fix the TeX",
        line: start.line ?? null,
        column: start.column ?? null,
        rule: "math-render-error",
      });
    }
  });
  return errors;
}

async function main() {
  const content = await readStdin();
  const errors = [];
  try {
    await compile(content, { remarkPlugins: [remarkCjkFriendly, remarkMath] });
  } catch (err) {
    errors.push(diagnostic(err));
  }
  // Only scan for render-crashing bare expressions when the parse/compile itself is clean;
  // a compile failure already pinpoints the syntax problem.
  if (errors.length === 0) {
    try {
      const tree = exprProcessor.parse(content);
      errors.push(...findBareExpressions(tree));
      errors.push(...findDisallowedJsx(tree));
      errors.push(...findBrokenMath(tree));
    } catch {
      // A scan failure must not mask an otherwise-clean compile.
    }
  }
  process.stdout.write(JSON.stringify({ ok: errors.length === 0, errors }));
}

main().catch((err) => {
  process.stderr.write(`mdx-validate internal error: ${err && err.stack ? err.stack : err}\n`);
  process.exit(2);
});
