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

test("no filter is the bare task path", () => {
  const none = of.originFiltersFromSearch("");
  assert.deepStrictEqual(none, { machine: null, user: null, worktree: null });
  assert.strictEqual(of.originFilterCount(none), 0);
  assert.strictEqual(of.taskListPath(none), "/api/tasks");
  assert.strictEqual(of.taskListPath(null), "/api/tasks");
});

test("empty parameters are no filter", () => {
  const f = of.originFiltersFromSearch("?machine=&user=&worktree=");
  assert.strictEqual(of.originFilterCount(f), 0);
});

test("filters are read from the page URL into the task list query", () => {
  const f = of.originFiltersFromSearch("?tag=x&machine=alice-laptop&user=human%3Aalice");
  assert.deepStrictEqual(f, { machine: "alice-laptop", user: "human:alice", worktree: null });
  assert.strictEqual(of.originFilterCount(f), 2);
  assert.strictEqual(of.taskListPath(f), "/api/tasks?machine=alice-laptop&user=human%3Aalice");
});

test("values with reserved characters round-trip", () => {
  const f = { machine: "a&b=c", user: null, worktree: "/srv/wt #1" };
  const back = new URLSearchParams(of.taskListPath(f).split("?")[1]);
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

// -- the task list gate: which /api/tasks response the page installs ---------

const M1 = { machine: "m1" };
const M2 = { machine: "m2" };
const NONE = { machine: null, user: null, worktree: null };

test("every request carries an explicit snapshot, empty included", () => {
  const gate = of.createTaskListGate(M1);
  const refresh = gate.request();
  assert.strictEqual(refresh.path, "/api/tasks?machine=m1");
  const clear = gate.request({});
  assert.deepStrictEqual(clear.filters, NONE);
  assert.strictEqual(clear.path, "/api/tasks");  // not the shown m1
  // The snapshot is a copy: changing the caller's object changes nothing.
  const f = { machine: "m2" };
  const change = gate.request(f);
  f.machine = "zz";
  assert.strictEqual(change.path, "/api/tasks?machine=m2");
});

test("clear: the empty request installs and becomes the shown filters", () => {
  const gate = of.createTaskListGate(M1);
  const clear = gate.request(of.originFiltersFromSearch(""));
  assert.deepStrictEqual(gate.active(), { machine: "m1", user: null, worktree: null });
  assert.strictEqual(gate.accept(clear), true);
  assert.deepStrictEqual(gate.active(), NONE);
});

test("failure: a failed change reverts, shown filters unchanged", () => {
  const gate = of.createTaskListGate(M1);
  const change = gate.request(M2);
  assert.strictEqual(gate.fail(change), true);
  assert.deepStrictEqual(gate.active().machine, "m1");
  // A refresh under the shown filters still installs afterwards.
  const refresh = gate.request();
  assert.strictEqual(refresh.path, "/api/tasks?machine=m1");
  assert.strictEqual(gate.accept(refresh), true);
  // An older change failing after a newer one was issued does not revert.
  const older = gate.request(M2);
  const newer = gate.request(NONE);
  assert.strictEqual(gate.fail(older), false);
  assert.strictEqual(gate.fail(newer), true);
});

test("out of order: the latest change wins whatever order responses arrive", () => {
  const gate = of.createTaskListGate(NONE);
  const first = gate.request(M1);
  const second = gate.request(M2);
  assert.strictEqual(gate.accept(second), true);
  assert.strictEqual(gate.accept(first), false);
  assert.strictEqual(gate.active().machine, "m2");

  const gate2 = of.createTaskListGate(NONE);
  const a = gate2.request(M1);
  const b = gate2.request(M2);
  assert.strictEqual(gate2.accept(a), false);  // superseded before it arrived
  assert.strictEqual(gate2.accept(b), true);
  assert.strictEqual(gate2.active().machine, "m2");
});

test("an unfiltered refresh never overwrites a newer filtered result", () => {
  const gate = of.createTaskListGate(NONE);
  const refresh = gate.request();  // sent unfiltered
  const change = gate.request(M2);
  assert.strictEqual(gate.accept(change), true);
  assert.strictEqual(gate.accept(refresh), false);  // arrives late: dropped
});

test("a refresh sent under the old filters never lands after the change", () => {
  const gate = of.createTaskListGate(M1);
  const change = gate.request(M2);
  const refresh = gate.request();  // issued while the change is in flight: m1
  assert.strictEqual(refresh.path, "/api/tasks?machine=m1");
  assert.strictEqual(gate.accept(change), true);
  assert.strictEqual(gate.accept(refresh), false);
});

test("a refresh arriving before a pending change installs, then the change still wins", () => {
  const gate = of.createTaskListGate(M1);
  const change = gate.request(M2);
  const refresh = gate.request();
  assert.strictEqual(gate.accept(refresh), true);  // still m1 on screen
  assert.strictEqual(gate.accept(change), true);
  assert.strictEqual(gate.active().machine, "m2");
});

test("refreshes install newest first only", () => {
  const gate = of.createTaskListGate(M1);
  const r1 = gate.request();
  const r2 = gate.request();
  assert.strictEqual(gate.accept(r2), true);
  assert.strictEqual(gate.accept(r1), false);
});

// -- disjoint selections AND with an origin filter (review round 2) ----------

const actor = require(
  path.join(__dirname, "..", "..", "src", "lattice", "dashboard", "static", "actor-logic.js")
);

// Task A: tag x, agent:a, written on m1. Task B: tag y, agent:b, on m2.
// With ?machine=m2 the server returns only B.
const TASK_B = { id: "B", tags: ["y"], assigned_to: "agent:b", created_by: "agent:b" };
const LOADED_UNDER_M2 = [TASK_B];

function tagValues(tasks) {
  const set = new Set();
  tasks.forEach((t) => (t.tags || []).forEach((tag) => set.add(tag)));
  return [...set];
}

function actorValues(tasks, field) {
  return [...new Set(tasks.map((t) => actor.normalizeActor(t[field])).filter((k) => k !== null))];
}

for (const [name, values, selections] of [
  ["tag", () => tagValues(LOADED_UNDER_M2), { tag: "x" }],
  ["assignee", () => actorValues(LOADED_UNDER_M2, "assigned_to"), { assignee: "agent:a" }],
  ["creator", () => actorValues(LOADED_UNDER_M2, "created_by"), { creator: "agent:a" }],
]) {
  test(`disjoint ${name} under an origin filter: kept selected, zero tasks shown`, () => {
    const active = Object.values(selections)[0];
    const res = of.resolveSelection(values(), active, M2);
    assert.strictEqual(res.selected, active);
    assert.ok(res.options.includes(active));
    const shown = LOADED_UNDER_M2.filter((t) =>
      of.taskMatchesSelections(t, selections, actor.actorMatchesFilter)
    );
    assert.deepStrictEqual(shown, []);
  });

  test(`absent ${name} without an origin filter clears as before`, () => {
    const active = Object.values(selections)[0];
    const res = of.resolveSelection(values(), active, NONE);
    assert.strictEqual(res.selected, null);
    assert.ok(!res.options.includes(active));
  });
}

test("a present selection stays and matches its tasks", () => {
  const res = of.resolveSelection(tagValues(LOADED_UNDER_M2), "y", M2);
  assert.deepStrictEqual(res, { options: ["y"], selected: "y" });
  const shown = LOADED_UNDER_M2.filter((t) =>
    of.taskMatchesSelections(t, { tag: "y", assignee: "agent:b" }, actor.actorMatchesFilter)
  );
  assert.deepStrictEqual(shown, [TASK_B]);
});
