"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const logic = require(path.join(__dirname, "..", "..", "src", "lattice", "dashboard", "static", "issue-view-logic.js"));

test("queue mapping keeps linked, resolved and both closure states in their round-four queues", () => {
  assert.deepEqual(logic.QUEUES.map((queue) => queue.key), ["open", "linked", "resolved", "closed"]);
  assert.equal(logic.queueOf({ state: "open" }), "open");
  assert.equal(logic.queueOf({ state: "linked" }), "linked");
  assert.equal(logic.queueOf({ state: "resolved" }), "resolved");
  assert.equal(logic.queueOf({ state: "dismissed" }), "closed");
  assert.equal(logic.queueOf({ state: "duplicate" }), "closed");
});

test("person view includes filed and commented rows and sorts by latest actor activity", () => {
  const rows = [
    { id: "old", seq: 1, filed_by: "agent:qa", filed_at: "2026-01-01T00:00:00Z", comments: [] },
    { id: "new", seq: 2, filed_by: "human:atin", comments: [{ author: "agent:qa", created_at: "2026-02-01T00:00:00Z" }] },
    { id: "miss", seq: 3, filed_by: "human:atin", comments: [] },
  ];
  assert.deepEqual(logic.sortPersonIssues(rows, "agent:qa").map((row) => row.id), ["new", "old"]);
  assert.equal(logic.matchedBy(rows[1], "agent:qa"), "commented");
  assert.equal(logic.matchedBy(rows[2], "agent:qa"), null);
});

test("server actor rows preserve actor counts, activity order and human versus agent origins", () => {
  const rows = [
    {
      id: "filed", seq: 1, filed_by: "human:atin", filed_at: "2026-03-01T00:00:00Z",
      filed_origin: { user: "atin", machine: "Atlas" }, actor_comment_count: 2,
      actor_comment_origins: [{ user: "atin", machine: "Atlas" }, { user: "atin", machine: "Hyperion" }],
    },
    {
      id: "commented", seq: 2, filed_by: "agent:other", matched_by: "commented",
      actor_activity_at: "2026-03-02T00:00:00Z", actor_comment_count: 1,
      actor_comment_origins: [{ user: "atin", machine: "Atlas" }],
    },
  ];
  assert.deepEqual(logic.sortPersonIssues(rows, "human:atin").map((row) => row.id), ["commented", "filed"]);
  assert.deepEqual(logic.personSummary(rows, "human:atin"), {
    filed: 1, comments: 3, machines: ["Atlas", "Hyperion"],
  });
});

test("issue list person rows retain their server-provided match marker", () => {
  const issue = { id: "i", matched_by: "commented", filed_by: "agent:other" };
  assert.equal(logic.personIssues([issue], "human:atin").length, 1);
  assert.equal(logic.matchedBy(issue, "human:atin"), "commented");
});

test("person activity flattens replies and preserves unknown origins", () => {
  const rows = [{
    id: "thread", filed_by: "agent:other", matched_by: "commented", actor_comment_count: 2,
    actor_comment_origins: [{ user: "atin", machine: "Atlas" }, null],
    comments: [{ author: "human:atin", replies: [{ author: "human:atin" }] }],
  }];
  assert.equal(logic.flattenComments(rows[0].comments).length, 2);
  assert.deepEqual(logic.personSummary(rows, "human:atin"), {
    filed: 0, comments: 2, machines: ["Atlas", "unknown"],
  });
});

test("video frame sample times match the issue media contract", () => {
  assert.deepEqual(logic.frameTimes(999), [0]);
  assert.deepEqual(logic.frameTimes(10_000), [0, 1980, 3960, 5940, 7920, 9900]);
  assert.equal(logic.frameTimes(60_000).length, 8);
});
