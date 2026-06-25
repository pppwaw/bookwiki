#!/usr/bin/env node
// Extract inline quiz structure from a chapter/section MDX body.
//
// BookWiki authors knowledge quizzes inline (SectionAgent writes a full <QuizBlock> with
// <QuizItem>s directly in the prose) and marks application quizzes with item-level
// placeholders (<QuizItemSlot ... />) inside an authored <QuizBlock>. The Python
// sanitizer needs a TRUSTWORTHY structural read of those tags — answers, choice ids,
// citations, slot specs — plus exact source offsets so it can drop/rewrite blocks in
// place. We parse with the SAME remark stack as tools/mdx-validate (remark-parse +
// remark-mdx + remark-cjk-friendly + remark-math) so what we extract matches what the
// site will actually parse, and so LaTeX `$...$` is consumed by remark-math (never seen
// as a JSX expression).
//
// Reads MDX from stdin, prints one JSON line:
//   {"ok": bool, "blocks": [...], "errors": [...]}
// `ok` only reflects whether parsing itself succeeded; per-item validity is the Python
// sanitizer's job. A non-zero exit code is reserved for internal toolchain failures.
import remarkCjkFriendly from "remark-cjk-friendly";
import remarkMath from "remark-math";
import remarkMdx from "remark-mdx";
import remarkParse from "remark-parse";
import { unified } from "unified";

const QUIZ_BLOCK = "QuizBlock";
const QUIZ_ITEM = "QuizItem";
const QUIZ_ITEM_SLOT = "QuizItemSlot";
const QUIZ_QUESTION = "QuizQuestion";
const QUIZ_CHOICES = "QuizChoices";
const QUIZ_CHOICE = "QuizChoice";
const QUIZ_EXPLANATION = "QuizExplanation";
const BOOK_FIGURE = "BookFigure";

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

const processor = unified()
  .use(remarkParse)
  .use(remarkMdx)
  .use(remarkCjkFriendly)
  .use(remarkMath);

function isJsxElement(node) {
  return (
    node &&
    (node.type === "mdxJsxFlowElement" || node.type === "mdxJsxTextElement")
  );
}

function jsxChildren(node, name) {
  return (node.children || []).filter(
    (child) => isJsxElement(child) && child.name === name,
  );
}

function firstJsxChild(node, name) {
  return jsxChildren(node, name)[0] || null;
}

function offsetsOf(node) {
  const start = node.position && node.position.start;
  const end = node.position && node.position.end;
  return {
    start: start && typeof start.offset === "number" ? start.offset : null,
    end: end && typeof end.offset === "number" ? end.offset : null,
  };
}

// Inner source (raw MDX, LaTeX preserved) between a node's first and last child.
function innerSource(node, source) {
  const kids = node.children || [];
  if (kids.length === 0) return "";
  const s = kids[0].position && kids[0].position.start;
  const e = kids[kids.length - 1].position && kids[kids.length - 1].position.end;
  if (!s || !e || typeof s.offset !== "number" || typeof e.offset !== "number") {
    return "";
  }
  return source.slice(s.offset, e.offset).trim();
}

function getAttr(node, name) {
  for (const attr of node.attributes || []) {
    if (attr.type === "mdxJsxAttribute" && attr.name === name) return attr;
  }
  return null;
}

// Plain string attribute value (e.g. answer="choice-1", id="auto"). Returns null when the
// attribute is missing, boolean, or an expression.
function stringAttr(node, name) {
  const attr = getAttr(node, name);
  if (!attr) return null;
  if (typeof attr.value === "string") return attr.value;
  if (
    attr.value &&
    typeof attr.value === "object" &&
    attr.value.type === "mdxJsxAttributeValueExpression"
  ) {
    const estree = attr.value.data && attr.value.data.estree;
    const stmt = estree && estree.body && estree.body[0];
    const expr = stmt && stmt.type === "ExpressionStatement" ? stmt.expression : null;
    if (expr && expr.type === "Literal" && typeof expr.value === "string") return expr.value;
  }
  return null;
}

// Safely evaluate a JS literal estree node into a JSON value. Only Literal / ArrayExpression
// / ObjectExpression are allowed (recursively). Spread, Identifier, Call, Template, etc. are
// rejected so a quiz can never smuggle executable JS into the pipeline.
function safeLiteral(node) {
  if (!node) return { ok: false, reason: "empty" };
  switch (node.type) {
    case "Literal":
      return { ok: true, value: node.value };
    case "ArrayExpression": {
      const out = [];
      for (const el of node.elements) {
        if (!el || el.type === "SpreadElement") {
          return { ok: false, reason: "array-spread-or-hole" };
        }
        const r = safeLiteral(el);
        if (!r.ok) return r;
        out.push(r.value);
      }
      return { ok: true, value: out };
    }
    case "ObjectExpression": {
      const out = {};
      for (const prop of node.properties) {
        if (prop.type !== "Property" || prop.computed || prop.kind !== "init") {
          return { ok: false, reason: "object-non-plain-property" };
        }
        let key = null;
        if (prop.key.type === "Identifier") key = prop.key.name;
        else if (prop.key.type === "Literal") key = String(prop.key.value);
        if (key === null) return { ok: false, reason: "object-bad-key" };
        const r = safeLiteral(prop.value);
        if (!r.ok) return r;
        out[key] = r.value;
      }
      return { ok: true, value: out };
    }
    default:
      return { ok: false, reason: `unsupported-node:${node.type}` };
  }
}

// Read an expression attribute (citations={[...]}, sourceRefs={[...]}) as a safe literal.
function exprAttr(node, name) {
  const attr = getAttr(node, name);
  if (!attr) return { present: false, ok: false, value: null, reason: "missing" };
  const value = attr.value;
  if (
    !value ||
    typeof value !== "object" ||
    value.type !== "mdxJsxAttributeValueExpression"
  ) {
    return { present: true, ok: false, value: null, reason: "not-expression" };
  }
  const estree = value.data && value.data.estree;
  const stmt = estree && estree.body && estree.body[0];
  if (!stmt || stmt.type !== "ExpressionStatement") {
    return { present: true, ok: false, value: null, reason: "no-expression" };
  }
  const r = safeLiteral(stmt.expression);
  return { present: true, ok: r.ok, value: r.ok ? r.value : null, reason: r.reason };
}

function parseChoice(node, source) {
  return { id: stringAttr(node, "id"), text: innerSource(node, source) };
}

function parseItem(node, source) {
  const question = firstJsxChild(node, QUIZ_QUESTION);
  const choicesWrap = firstJsxChild(node, QUIZ_CHOICES);
  const explanation = firstJsxChild(node, QUIZ_EXPLANATION);
  const figure = firstJsxChild(node, BOOK_FIGURE);
  const choices = choicesWrap
    ? jsxChildren(choicesWrap, QUIZ_CHOICE).map((c) => parseChoice(c, source))
    : [];
  return {
    kind: "item",
    ...offsetsOf(node),
    answer: stringAttr(node, "answer"),
    figure_ref: figure ? stringAttr(figure, "id") : "",
    question: question ? innerSource(question, source) : "",
    explanation: explanation ? innerSource(explanation, source) : "",
    choices,
    citations: exprAttr(node, "citations"),
  };
}

function parseSlot(node) {
  return {
    kind: "slot",
    ...offsetsOf(node),
    id: stringAttr(node, "id"),
    topic: stringAttr(node, "topic"),
    concept: stringAttr(node, "concept"),
    slotKind: stringAttr(node, "kind"),
    sourceRefs: exprAttr(node, "sourceRefs"),
  };
}

function parseBlock(node, source) {
  const children = [];
  for (const child of node.children || []) {
    if (!isJsxElement(child)) continue; // ignore whitespace / stray text
    if (child.name === QUIZ_ITEM) children.push(parseItem(child, source));
    else if (child.name === QUIZ_ITEM_SLOT) children.push(parseSlot(child));
    else children.push({ kind: "unknown", name: child.name || "", ...offsetsOf(child) });
  }
  return { ...offsetsOf(node), children };
}

// Walk only top-level / flow position; collect every <QuizBlock> anywhere in the tree.
function collectBlocks(tree, source) {
  const blocks = [];
  const walk = (node) => {
    if (isJsxElement(node) && node.name === QUIZ_BLOCK) {
      blocks.push(parseBlock(node, source));
      return; // do not descend into a quiz block again
    }
    for (const child of node.children || []) walk(child);
  };
  walk(tree);
  return blocks;
}

// remark ``position.offset`` values are UTF-16 code-unit indices (JS string indices), but the
// Python sanitizer slices by CODE POINTS. Convert every emitted offset to a code-point index so
// a non-BMP character (e.g. 𝑋/𝜇 math-alphanumerics, emoji, CJK Ext-B) before a quiz block does
// not drift the Python slice. This runs AFTER collectBlocks, so the JS-side `innerSource`
// slicing (which correctly uses UTF-16 offsets) is unaffected.
function buildOffsetConverter(source) {
  const map = new Map([[0, 0]]);
  let u16 = 0;
  let cp = 0;
  for (const ch of source) {
    u16 += ch.length; // 1 for BMP, 2 for an astral (surrogate-pair) code point
    cp += 1;
    map.set(u16, cp);
  }
  return (offset) => (map.has(offset) ? map.get(offset) : cp);
}

function convertBlockOffsets(blocks, toCodePoint) {
  for (const block of blocks) {
    if (typeof block.start === "number") block.start = toCodePoint(block.start);
    if (typeof block.end === "number") block.end = toCodePoint(block.end);
    for (const child of block.children || []) {
      if (typeof child.start === "number") child.start = toCodePoint(child.start);
      if (typeof child.end === "number") child.end = toCodePoint(child.end);
    }
  }
}

async function main() {
  const source = await readStdin();
  let tree;
  try {
    tree = processor.parse(source);
  } catch (err) {
    process.stdout.write(
      JSON.stringify({
        ok: false,
        blocks: [],
        errors: [String((err && (err.reason || err.message)) || err)],
      }),
    );
    return;
  }
  const blocks = collectBlocks(tree, source);
  convertBlockOffsets(blocks, buildOffsetConverter(source));
  process.stdout.write(JSON.stringify({ ok: true, blocks, errors: [] }));
}

main().catch((err) => {
  process.stderr.write(
    `mdx-quiz-extract internal error: ${err && err.stack ? err.stack : err}\n`,
  );
  process.exit(2);
});
