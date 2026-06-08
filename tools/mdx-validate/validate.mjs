#!/usr/bin/env node
// Compile-check MDX with the SAME remark config as the fumadocs site (remark-math),
// so generation-time validation matches what the site's MDX parser will accept.
//
// Reads MDX from stdin, prints a JSON line: {"ok": bool, "errors": [{message,line,column}]}.
// A non-zero exit code is reserved for internal failures (e.g. the toolchain itself broke),
// NOT for invalid MDX — invalid MDX is a normal result reported via {"ok": false, ...}.
import { compile } from "@mdx-js/mdx";
import remarkMath from "remark-math";

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

async function main() {
  const content = await readStdin();
  try {
    await compile(content, { remarkPlugins: [remarkMath] });
    process.stdout.write(JSON.stringify({ ok: true, errors: [] }));
  } catch (err) {
    process.stdout.write(JSON.stringify({ ok: false, errors: [diagnostic(err)] }));
  }
}

main().catch((err) => {
  process.stderr.write(`mdx-validate internal error: ${err && err.stack ? err.stack : err}\n`);
  process.exit(2);
});
