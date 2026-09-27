"use strict";

// Tests for the dashboard's escaping and base-path helpers (escape.js), and a
// static scan proving no page script builds an inline event handler (SPEC
// §10, AC-24). Runs under node's built-in test runner — zero npm deps, no
// package.json. Bridged into pytest via tests/test_dashboard/test_js_escape.py
// so `uv run pytest` stays the single entrypoint. Run standalone with:
//   node --test tests/js/escape.test.js

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const STATIC = path.join(__dirname, "..", "..", "src", "lattice", "dashboard", "static");
const { esc, basePath, apiUrl } = require(path.join(STATIC, "escape.js"));

// Every script the page loads from this repo, plus the page itself.
const PAGE_FILES = [
  "index.html",
  "cube3d.js",
  "cube-v2.js",
  "lane-logic.js",
  "actor-logic.js",
  "panel-logic.js",
  "escape.js",
];

const HOSTILE = [
  "'",
  '"',
  "<script>alert(1)</script>",
  "' onmouseover='alert(1)",
  '" onclick="alert(1)',
  "x');alert(1);('",
  "\u001b[31mred\u001b[0m",
  "&amp;",
];

test("esc: escapes the five HTML-significant characters", () => {
  assert.equal(esc("&<>\"'"), "&amp;&lt;&gt;&quot;&#39;");
  assert.equal(esc("O'Brien"), "O&#39;Brien");
  assert.equal(esc("plain text"), "plain text");
});

test("esc: null and undefined are empty; other values are stringified", () => {
  assert.equal(esc(null), "");
  assert.equal(esc(undefined), "");
  assert.equal(esc(0), "0");
  assert.equal(esc(false), "false");
});

test("esc: hostile board text cannot leave a quoted attribute or open a tag", () => {
  for (const s of HOSTILE) {
    const out = esc(s);
    assert.ok(!/[<>"']/.test(out), `unescaped markup in ${JSON.stringify(out)}`);
    const dbl = '<span data-task-id="' + out + '">';
    const sgl = "<span data-task-id='" + out + "'>";
    assert.equal((dbl.match(/"/g) || []).length, 2, dbl);
    assert.equal((sgl.match(/'/g) || []).length, 2, sgl);
  }
});

test("esc: ampersands are escaped first, so entities are not double-decoded", () => {
  assert.equal(esc("&#39;"), "&amp;#39;");
});

test("basePath: the page's directory, always slash-terminated", () => {
  assert.equal(basePath("/"), "/");
  assert.equal(basePath("/index.html"), "/");
  assert.equal(basePath("/p/proj/"), "/p/proj/");
  assert.equal(basePath("/p/proj/index.html"), "/p/proj/");
  assert.equal(basePath(""), "/");
  assert.equal(basePath(undefined), "/");
  assert.equal(basePath("relative/path"), "/");
});

test("apiUrl: API paths resolve under the base path", () => {
  assert.equal(apiUrl("/", "/api/tasks"), "/api/tasks");
  assert.equal(apiUrl("/", "api/tasks"), "/api/tasks");
  assert.equal(apiUrl("/p/proj/", "/api/tasks/x/events"), "/p/proj/api/tasks/x/events");
  assert.equal(apiUrl("/p/proj/", "/api/activity?task=A-1"), "/p/proj/api/activity?task=A-1");
});

// An inline event-handler attribute in markup or in a string that becomes
// markup: `onclick="…"`, `onclick=\"…\"`, `ondrop='…'`. A handler property
// assigned in code (`el.onload = fn`) is preceded by "." and is not matched.
const INLINE_HANDLER = /(^|[\s"'\\;<])on[a-z]+\s*=\s*\\?["']/i;

test("no page file contains an inline event handler or a javascript: URL", () => {
  for (const name of PAGE_FILES) {
    const lines = fs.readFileSync(path.join(STATIC, name), "utf8").split("\n");
    lines.forEach((line, i) => {
      assert.ok(!INLINE_HANDLER.test(line), `${name}:${i + 1} inline handler: ${line.trim()}`);
      assert.ok(!/javascript:/i.test(line), `${name}:${i + 1} javascript: URL: ${line.trim()}`);
    });
  }
});

test("the inline-handler scan catches the shapes it must", () => {
  assert.ok(INLINE_HANDLER.test(`<button onclick="go()">`));
  assert.ok(INLINE_HANDLER.test(`html += ' onclick="openPlans(\\'' + esc(id) + '\\')"';`));
  assert.ok(INLINE_HANDLER.test(`'<div ondragover=\\"x(event)\\">'`));
  assert.ok(INLINE_HANDLER.test(`<a onmouseover='x'>`));
  assert.ok(!INLINE_HANDLER.test(`img.onload = function() {};`));
  assert.ok(!INLINE_HANDLER.test(`<meta name="viewport" content="width=device-width">`));
});

test("every data-action the page renders has a handler", () => {
  const index = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
  const declared = new Set();
  const table = index.slice(index.indexOf("var PAGE_ACTIONS = {"));
  const body = table.slice(0, table.indexOf("\n};"));
  for (const m of body.matchAll(/^\s*"([a-z-]+)":\s*function/gm)) declared.add(m[1]);
  assert.ok(declared.size > 0, "PAGE_ACTIONS table not found");
  for (const name of PAGE_FILES) {
    const text = fs.readFileSync(path.join(STATIC, name), "utf8");
    for (const m of text.matchAll(/data-action=\\?"([a-z-]+)\\?"/g)) {
      assert.ok(declared.has(m[1]), `${name}: data-action="${m[1]}" has no PAGE_ACTIONS entry`);
    }
  }
});
