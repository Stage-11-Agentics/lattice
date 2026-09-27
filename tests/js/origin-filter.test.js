"use strict";

// Tests for the dashboard's origin filter query logic (origin-filter.js). Runs
// under node's built-in test runner, bridged into pytest via
// tests/test_dashboard/test_js_origin_filter.py. Run standalone with:
//   node --test tests/js/origin-filter.test.js

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const of = require(
  path.join(__dirname, "..", "..", "src", "lattice", "dashboard", "static", "origin-filter.js")
);

test("no filter leaves the task path unchanged", () => {
  const none = of.originFiltersFromSearch("");
  assert.deepStrictEqual(none, { machine: null, user: null, worktree: null });
  assert.strictEqual(of.originFilterCount(none), 0);
  assert.strictEqual(of.withOriginFilters("/api/tasks", none), "/api/tasks");
  assert.strictEqual(of.withOriginFilters("/api/tasks", null), "/api/tasks");
});

test("empty parameters are no filter", () => {
  const f = of.originFiltersFromSearch("?machine=&user=&worktree=");
  assert.strictEqual(of.originFilterCount(f), 0);
});

test("filters are read from the page URL and sent to /api/tasks only", () => {
  const f = of.originFiltersFromSearch("?tag=x&machine=alice-laptop&user=human%3Aalice");
  assert.deepStrictEqual(f, { machine: "alice-laptop", user: "human:alice", worktree: null });
  assert.strictEqual(of.originFilterCount(f), 2);
  assert.strictEqual(
    of.withOriginFilters("/api/tasks", f),
    "/api/tasks?machine=alice-laptop&user=human%3Aalice"
  );
  assert.strictEqual(of.withOriginFilters("/api/archived", f), "/api/archived");
  assert.strictEqual(of.withOriginFilters("/api/tasks/task_x", f), "/api/tasks/task_x");
});

test("values with reserved characters round-trip", () => {
  const f = { machine: "a&b=c", user: null, worktree: "/srv/wt #1" };
  const qs = of.withOriginFilters("/api/tasks", f).split("?")[1];
  const back = new URLSearchParams(qs);
  assert.strictEqual(back.get("machine"), "a&b=c");
  assert.strictEqual(back.get("worktree"), "/srv/wt #1");
  assert.strictEqual(back.get("user"), null);
});

test("worktree paths normalize as the CLI's --worktree does for absolute paths", () => {
  const cases = [
    ["/srv/wt/", "/srv/wt"],
    ["/srv//wt", "/srv/wt"],
    ["/srv/./wt/.", "/srv/wt"],
    ["/srv/x/../wt", "/srv/wt"],
    ["/..", "/"],
    ["/", "/"],
    ["///srv", "/srv"],
    ["//srv/wt", "/srv/wt"],
    ["relative/wt", "relative/wt"],
  ];
  for (const [input, expected] of cases) {
    assert.strictEqual(of.normalizeWorktree(input), expected, input);
  }
  assert.strictEqual(of.originFiltersFromSearch("?worktree=%2Fsrv%2Fwt%2F").worktree, "/srv/wt");
});

test("an absent tag/actor selection is kept only under an origin filter", () => {
  assert.strictEqual(of.keepsAbsentSelection(of.originFiltersFromSearch("")), false);
  assert.strictEqual(of.keepsAbsentSelection(null), false);
  assert.strictEqual(of.keepsAbsentSelection(of.originFiltersFromSearch("?tag=x&machine=m2")), true);
});
