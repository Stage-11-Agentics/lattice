"use strict";

// Tests for the dashboard's pure panel-dismissal logic (panel-logic.js). Runs
// under node's built-in test runner — zero npm deps, no package.json. Bridged
// into pytest via tests/test_dashboard/test_js_panel_logic.py so
// `uv run pytest` stays the single entrypoint. Run standalone with:
//   node --test tests/js/panel-logic.test.js
//
// Regression coverage for GitHub #48: the board detail panel closed when an
// inline-edit control swapped itself out of the DOM before the click reached
// the document-level click-outside handler.

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const { clickPathMatches, clickPath } = require(
  path.join(__dirname, "..", "..", "src", "lattice", "dashboard", "static", "panel-logic.js")
);

// Minimal element stand-in: matches by id ("#x") or class (".y") only.
function el(id, classes, parent) {
  const node = {
    id: id || "",
    classes: classes || [],
    parentNode: parent || null,
    matches(sel) {
      if (sel[0] === "#") return this.id === sel.slice(1);
      if (sel[0] === ".") return this.classes.includes(sel.slice(1));
      throw new Error("unsupported selector in test stub: " + sel);
    },
  };
  return node;
}

test("clickPathMatches: hit anywhere on the path", () => {
  const panel = el("detail-panel");
  const span = el("dp-assigned", ["editable"]);
  assert.equal(clickPathMatches([span, panel, {}, null], ["#detail-panel"]), true);
  assert.equal(clickPathMatches([panel, span], [".editable"]), true);
});

test("clickPathMatches: no hit when nothing on the path matches", () => {
  const board = el("board", ["view"]);
  assert.equal(clickPathMatches([board, {}, null], ["#detail-panel", ".card"]), false);
});

test("clickPathMatches: detached target still counts when the panel is on the path", () => {
  // The #48 scenario: composedPath() captured [span, dd, panel, body, ...] at
  // dispatch; span.parentNode is now null but the panel is still on the path.
  const panel = el("detail-panel");
  const span = el("dp-assigned", ["editable"], null);
  assert.equal(clickPathMatches([span, panel], ["#detail-panel"]), true);
});

test("clickPathMatches: never throws on junk", () => {
  assert.equal(clickPathMatches(null, ["#x"]), false);
  assert.equal(clickPathMatches([], ["#x"]), false);
  assert.equal(clickPathMatches([el("x")], null), false);
  assert.equal(clickPathMatches([el("x")], []), false);
  assert.equal(clickPathMatches([1, "s", undefined, { matches: 3 }], ["#x"]), false);
  // A selector the node rejects (our stub throws on unknown forms) is a miss, not a crash.
  assert.equal(clickPathMatches([el("x")], ["div > span"]), false);
});

test("clickPath: prefers composedPath when present", () => {
  const panel = el("detail-panel");
  const span = el("dp-assigned");
  const ev = { target: span, composedPath: () => [span, panel] };
  assert.deepEqual(clickPath(ev), [span, panel]);
});

test("clickPath: falls back to walking parentNode", () => {
  const body = el("", ["body"]);
  const panel = el("detail-panel", [], body);
  const span = el("dp-assigned", [], panel);
  assert.deepEqual(clickPath({ target: span }), [span, panel, body]);
  assert.deepEqual(clickPath({ target: span, composedPath: () => [] }), [span, panel, body]);
});

test("clickPath: empty for a missing event or target", () => {
  assert.deepEqual(clickPath(null), []);
  assert.deepEqual(clickPath({}), []);
});
