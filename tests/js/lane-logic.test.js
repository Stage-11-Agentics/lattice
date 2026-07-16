"use strict";

// Tests for the dashboard's pure lane sort/group logic. Runs under node's built-in
// test runner — zero npm deps, no package.json. Bridged into pytest via
// tests/test_dashboard/test_js_lane_logic.py so `uv run pytest` stays the single
// entrypoint. Run standalone with: node --test tests/js/lane-logic.test.js
//
// These cover the regressions PR #33's second commit fixed (prototype-pollution-safe
// buckets, junk-mode fallback, legacy done_display derivation) plus the surrounding
// sort/group contract. This is a harness over moved-not-rewritten logic; the
// non-transitive _byTime missing-field fallback is a documented, accepted tradeoff.

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const lane = require(
  path.join(__dirname, "..", "..", "src", "lattice", "dashboard", "static", "lane-logic.js")
);
const {
  DEFAULT_LANE_SORT,
  laneModesFor,
  isValidLaneMode,
  resolveLaneSort,
  sortLaneItems,
  groupLaneItems,
} = lane;

// Small helper: pull the flat, ordered list of ids out of grouped sections.
function idsOf(items) {
  return items.map(function (t) { return t.id; });
}

// --- groupLaneItems: prototype-pollution-safe buckets ------------------------------

test("groupLaneItems: tags constructor / __proto__ / untagged → 3 sections, no throw, no loss", () => {
  const items = [
    { id: "a", tags: ["constructor"] },
    { id: "b", tags: ["__proto__"] },
    { id: "c", tags: [] },
  ];
  let sections;
  assert.doesNotThrow(() => { sections = groupLaneItems(items, "group:tag"); });
  assert.strictEqual(sections.length, 3);

  // Every card appears exactly once across all sections — no duplication, no loss.
  const allIds = sections.flatMap((s) => idsOf(s.items)).sort();
  assert.deepStrictEqual(allIds, ["a", "b", "c"]);

  const labels = sections.map((s) => s.label);
  assert.ok(labels.includes("constructor"));
  assert.ok(labels.includes("__proto__"));
  // Empty bucket labeled "Untagged" and sunk to the bottom.
  assert.strictEqual(sections[sections.length - 1].label, "Untagged");
});

test("groupLaneItems: actor id __proto__ groups; empty bucket 'Unassigned' sinks last", () => {
  const items = [
    { id: "a", assigned_to: "__proto__" },
    { id: "b", assigned_to: "constructor" },
    { id: "c" }, // unassigned
  ];
  let sections;
  assert.doesNotThrow(() => { sections = groupLaneItems(items, "group:assignee"); });
  assert.strictEqual(sections.length, 3);
  const labels = sections.map((s) => s.label);
  assert.ok(labels.includes("__proto__"));
  assert.ok(labels.includes("constructor"));
  assert.strictEqual(sections[sections.length - 1].label, "Unassigned");
});

test("groupLaneItems: multi-tag card appears exactly once, under alphabetically-first tag", () => {
  const items = [{ id: "a", tags: ["zebra", "apple", "mango"] }];
  const sections = groupLaneItems(items, "group:tag");
  const withCard = sections.filter((s) => idsOf(s.items).includes("a"));
  assert.strictEqual(withCard.length, 1, "card must appear in exactly one section");
  assert.strictEqual(withCard[0].label, "apple", "grouped under alphabetically-first tag");
});

test("groupLaneItems: named sections alphabetical, empty bucket always last", () => {
  const items = [
    { id: "a", tags: ["mango"] },
    { id: "b", tags: [] },
    { id: "c", tags: ["apple"] },
  ];
  const sections = groupLaneItems(items, "group:tag");
  assert.deepStrictEqual(sections.map((s) => s.label), ["apple", "mango", "Untagged"]);
});

// --- sortLaneItems: junk-mode fallback ---------------------------------------------

test("sortLaneItems: junk mode 'constructor' falls back to default order, never inherited fn", () => {
  const items = [
    { id: "b", created_at: "2026-01-02" },
    { id: "a", created_at: "2026-01-01" },
  ];
  const junk = sortLaneItems(items, "constructor");
  const def = sortLaneItems(items, DEFAULT_LANE_SORT);
  assert.deepStrictEqual(idsOf(junk), idsOf(def), "junk mode must match default ordering");
  // created_asc default: oldest (a) first.
  assert.deepStrictEqual(idsOf(junk), ["a", "b"]);
});

test("sortLaneItems: junk mode 'hasOwnProperty' also falls back to default", () => {
  const items = [
    { id: "b", created_at: "2026-01-02" },
    { id: "a", created_at: "2026-01-01" },
  ];
  const junk = sortLaneItems(items, "hasOwnProperty");
  assert.deepStrictEqual(idsOf(junk), ["a", "b"]);
});

// --- rank sorts --------------------------------------------------------------------

test("sortLaneItems: priority — critical first, unset last (rank 99)", () => {
  const items = [
    { id: "d" }, // unset priority
    { id: "c", priority: "low" },
    { id: "a", priority: "critical" },
    { id: "b", priority: "high" },
  ];
  const out = sortLaneItems(items, "priority");
  assert.deepStrictEqual(idsOf(out), ["a", "b", "c", "d"]);
});

test("sortLaneItems: urgency — immediate first, unset last", () => {
  const items = [
    { id: "c" }, // unset
    { id: "a", urgency: "immediate" },
    { id: "b", urgency: "normal" },
  ];
  assert.deepStrictEqual(idsOf(sortLaneItems(items, "urgency")), ["a", "b", "c"]);
});

test("sortLaneItems: complexity_asc — low first, unset last", () => {
  const items = [
    { id: "c" }, // unset
    { id: "a", complexity: "low" },
    { id: "b", complexity: "high" },
  ];
  assert.deepStrictEqual(idsOf(sortLaneItems(items, "complexity_asc")), ["a", "b", "c"]);
});

// --- _byTime semantics via modes ---------------------------------------------------

test("sortLaneItems: created_asc vs created_desc direction", () => {
  const items = [
    { id: "b", created_at: "2026-01-02" },
    { id: "a", created_at: "2026-01-01" },
    { id: "c", created_at: "2026-01-03" },
  ];
  assert.deepStrictEqual(idsOf(sortLaneItems(items, "created_asc")), ["a", "b", "c"]);
  assert.deepStrictEqual(idsOf(sortLaneItems(items, "created_desc")), ["c", "b", "a"]);
});

test("sortLaneItems: missing-field pair falls back to id-vs-id (never ULID-vs-ISO)", () => {
  // Neither has created_at → both compared by id. ids chosen so id order is a<b.
  const items = [
    { id: "b" },
    { id: "a" },
  ];
  // created_asc: id ascending → a, b.
  assert.deepStrictEqual(idsOf(sortLaneItems(items, "created_asc")), ["a", "b"]);
  // created_desc: dir flips the id comparison → b, a.
  assert.deepStrictEqual(idsOf(sortLaneItems(items, "created_desc")), ["b", "a"]);
});

test("sortLaneItems: status_age_desc = timestamp-ascending on last_status_changed_at", () => {
  // Oldest transition (longest sitting) comes first.
  const items = [
    { id: "b", last_status_changed_at: "2026-01-02" },
    { id: "a", last_status_changed_at: "2026-01-01" },
    { id: "c", last_status_changed_at: "2026-01-03" },
  ];
  assert.deepStrictEqual(idsOf(sortLaneItems(items, "status_age_desc")), ["a", "b", "c"]);
});

test("sortLaneItems: updated_desc — newest updated_at first", () => {
  // Pins the comparator to the real field name: a typo'd field would pass silently
  // via the id fallback, so ids are chosen to disagree with the expected order.
  const items = [
    { id: "a", updated_at: "2026-01-01" },
    { id: "b", updated_at: "2026-01-03" },
    { id: "c", updated_at: "2026-01-02" },
  ];
  assert.deepStrictEqual(idsOf(sortLaneItems(items, "updated_desc")), ["b", "c", "a"]);
});

// --- tie-break stability & no mutation ---------------------------------------------

test("sortLaneItems: equal sort keys break on id, deterministic", () => {
  const items = [
    { id: "c", priority: "high" },
    { id: "a", priority: "high" },
    { id: "b", priority: "high" },
  ];
  assert.deepStrictEqual(idsOf(sortLaneItems(items, "priority")), ["a", "b", "c"]);
});

test("sortLaneItems: input array is never mutated", () => {
  const items = [
    { id: "b", created_at: "2026-01-02" },
    { id: "a", created_at: "2026-01-01" },
  ];
  const before = idsOf(items);
  sortLaneItems(items, "created_asc");
  assert.deepStrictEqual(idsOf(items), before, "original order preserved (slice before sort)");
});

// --- resolveLaneSort: config-independent core --------------------------------------

test("resolveLaneSort: valid explicit map entry wins", () => {
  assert.strictEqual(resolveLaneSort("in_progress", { in_progress: "priority" }, null), "priority");
});

test("resolveLaneSort: junk map entry ignored → default", () => {
  assert.strictEqual(
    resolveLaneSort("in_progress", { in_progress: "constructor" }, null),
    DEFAULT_LANE_SORT
  );
});

test("resolveLaneSort: done-lane legacy derivation grouped→group:day, recent→recent, all→created_asc, unknown→default", () => {
  assert.strictEqual(resolveLaneSort("done", {}, "grouped"), "group:day");
  assert.strictEqual(resolveLaneSort("done", {}, "recent"), "recent");
  assert.strictEqual(resolveLaneSort("done", {}, "all"), "created_asc");
  assert.strictEqual(resolveLaneSort("done", {}, "nonsense"), DEFAULT_LANE_SORT);
  // Absent done_display defaults to "grouped" → group:day.
  assert.strictEqual(resolveLaneSort("done", {}, null), "group:day");
});

test("resolveLaneSort: explicit valid done mode wins over legacy derivation", () => {
  assert.strictEqual(resolveLaneSort("done", { done: "recent" }, "grouped"), "recent");
});

test("resolveLaneSort: done-only modes are invalid for non-done lanes → default", () => {
  assert.strictEqual(
    resolveLaneSort("in_progress", { in_progress: "group:day" }, null),
    DEFAULT_LANE_SORT
  );
  assert.strictEqual(
    resolveLaneSort("in_progress", { in_progress: "recent" }, null),
    DEFAULT_LANE_SORT
  );
});

// --- laneModesFor / isValidLaneMode ------------------------------------------------

test("laneModesFor: done includes done-only modes; other statuses exclude them", () => {
  const doneValues = laneModesFor("done").map((m) => m.value);
  assert.ok(doneValues.includes("group:day"));
  assert.ok(doneValues.includes("recent"));

  const otherValues = laneModesFor("in_progress").map((m) => m.value);
  assert.ok(!otherValues.includes("group:day"));
  assert.ok(!otherValues.includes("recent"));
});

test("isValidLaneMode: done-only modes valid only for done lane", () => {
  assert.strictEqual(isValidLaneMode("done", "group:day"), true);
  assert.strictEqual(isValidLaneMode("in_progress", "group:day"), false);
  assert.strictEqual(isValidLaneMode("in_progress", "priority"), true);
});
