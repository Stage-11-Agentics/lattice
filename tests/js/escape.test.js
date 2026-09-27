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
const {
  esc,
  classToken,
  ownValue,
  statusDisplayName,
  legendItemHtml,
  boardColumnOpenTag,
  basePath,
  apiUrl,
} = require(path.join(STATIC, "escape.js"));

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

// Hostile workflow labels and statuses (review round 1): they must render as
// text and never open a tag, leave an attribute, or add an event handler.
const HOSTILE_LABELS = [
  "</span><img src=x onerror=alert(1)>",
  'x"onmouseover="alert(1)',
  "x' onmouseover='alert(1)",
  "in progress<script>alert(1)</script>",
  "a b\tc",
  "constructor",
  "__proto__",
];

// Every tag in `html` is one of `tags`, its attributes are well-formed quoted
// name="value" pairs from `attrs`, no attribute is an event handler, every
// class is a safe token list, and no text holds a raw "<" or ">".
function assertSafeMarkup(html, tags, attrs) {
  const tagRe = /<\/?([a-zA-Z0-9]+)([^>]*)>/g;
  let m;
  let last = 0;
  while ((m = tagRe.exec(html))) {
    const text = html.slice(last, m.index);
    assert.ok(!/[<>]/.test(text), `raw markup in text ${JSON.stringify(text)}`);
    last = tagRe.lastIndex;
    assert.ok(tags.includes(m[1].toLowerCase()), `unexpected tag <${m[1]}> in ${html}`);
    const attrText = m[2];
    assert.ok(/^(\s+[a-z-]+="[^"<>]*")*\s*$/.test(attrText), `malformed attributes: ${attrText}`);
    for (const a of attrText.matchAll(/([a-z-]+)="([^"]*)"/g)) {
      assert.ok(attrs.includes(a[1]), `unexpected attribute ${a[1]} in ${html}`);
      assert.ok(!/^on/i.test(a[1]), `event handler attribute ${a[1]}`);
      if (a[1] === "class") assert.ok(/^[a-z0-9_ -]*$/.test(a[2]), `unsafe class "${a[2]}"`);
    }
  }
  assert.ok(!/[<>]/.test(html.slice(last)), "raw markup after the last tag");
}

test("classToken: only [a-z0-9_-], whitespace as _", () => {
  assert.equal(classToken("in_progress"), "in_progress");
  assert.equal(classToken("In Progress"), "in_progress");
  assert.equal(classToken('x"onmouseover="alert(1)'), "x-onmouseover--alert-1-");
  assert.equal(classToken(null), "");
  for (const s of HOSTILE_LABELS) assert.ok(/^[a-z0-9_-]*$/.test(classToken(s)), s);
});

test("ownValue: never reaches Object.prototype", () => {
  assert.equal(ownValue({ a: 1 }, "a"), 1);
  assert.equal(ownValue({}, "constructor"), undefined);
  assert.equal(ownValue({}, "__proto__"), undefined);
  assert.equal(ownValue(null, "a"), undefined);
});

test("statusDisplayName: display_names entry, else the slug; prototype keys ignored", () => {
  const wf = { display_names: { in_progress: "Doing" } };
  assert.equal(statusDisplayName(wf, "in_progress"), "Doing");
  assert.equal(statusDisplayName(wf, "needs_review"), "needs review");
  assert.equal(statusDisplayName(wf, "constructor"), "constructor");
  assert.equal(statusDisplayName(undefined, "x_y"), "x y");
  assert.equal(statusDisplayName(wf, ""), "");
});

test("legendItemHtml: hostile display names and colours render as text", () => {
  for (const label of HOSTILE_LABELS) {
    const wf = { display_names: { s: label } };
    const html = legendItemHtml(
      "cv2-legend-item",
      "cv2-legend-dot",
      label,
      statusDisplayName(wf, "s")
    );
    assertSafeMarkup(html, ["div", "span"], ["class", "style"]);
    assert.ok(html.includes(esc(label)), html);
  }
});

test("boardColumnOpenTag: a hostile status is a class token and an escaped data attribute", () => {
  for (const status of HOSTILE_LABELS) {
    const html = boardColumnOpenTag(status, status.length % 2 === 0);
    assertSafeMarkup(html, ["div"], ["class", "data-status"]);
    assert.ok(html.includes(' data-status="' + esc(status) + '"'), html);
  }
  assert.equal(
    boardColumnOpenTag("in_progress", true),
    '<div class="board-col status-in_progress empty-col" data-status="in_progress">'
  );
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
