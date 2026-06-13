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
