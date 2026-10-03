"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const logic = require(path.join(__dirname, "..", "..", "src", "lattice", "dashboard", "static", "issue-view-logic.js"));

test("queue mapping keeps linked, resolved and both closure states in their round-four queues", () => {
  assert.deepEqual(logic.QUEUES.map((queue) => queue.key), ["open", "linked", "resolved", "closed"]);
  assert.deepEqual(logic.QUEUES.map((queue) => queue.label), ["No story", "Has story", "Resolved", "Closed"]);
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

test("person summary counts filings and comments and lists machines most used first", () => {
  const rows = [
    {
      id: "filed", seq: 1, filed_by: "human:atin", filed_at: "2026-03-01T00:00:00Z", matched_by: "filed",
      filed_origin: { user: "atin", machine: "Atlas" }, actor_comment_count: 3,
      actor_comment_origins: [{ user: "atin", machine: "Hyperion" }, { user: "atin", machine: "Hyperion" }, { user: "atin", machine: "Hyperion" }],
    },
    {
      id: "commented", seq: 2, filed_by: "agent:other", matched_by: "commented",
      actor_activity_at: "2026-03-02T00:00:00Z", actor_comment_count: 1,
      actor_comment_origins: [{ user: "root", machine: "Atlas" }],
    },
  ];
  assert.deepEqual(logic.sortPersonIssues(rows, "human:atin").map((row) => row.id), ["commented", "filed"]);
  // Alphabetical would be Atlas first; Hyperion was used three times.
  assert.deepEqual(logic.personSummary(rows, "human:atin"), {
    filed: 1, comments: 4, machines: ["Hyperion", "Atlas", "root@Atlas"],
  });
});

test("person summary skips missing origins, as the prototype does", () => {
  const rows = [{
    id: "thread", filed_by: "agent:other", matched_by: "commented", actor_comment_count: 2,
    actor_comment_origins: [{ user: "atin", machine: "Atlas" }, null],
    comments: [{ author: "human:atin", replies: [{ author: "human:atin" }] }],
  }];
  assert.equal(logic.flattenComments(rows[0].comments).length, 2);
  assert.deepEqual(logic.personSummary(rows, "human:atin"), { filed: 0, comments: 2, machines: ["Atlas"] });
});

test("origins: an event's raw origin reads its reported user and host, an authenticated pair wins", () => {
  const raw = { op: "issue.comment", op_id: "op_1", reported: { host: "Atlas", os_user: "atin", worktree: "/x" } };
  assert.deepEqual(logic.originPair(raw), { user: "atin", machine: "Atlas" });
  assert.equal(logic.originLabel("human:atin", raw), "Atlas");
  assert.equal(logic.originLabel("agent:qa", raw), "atin@Atlas");
  const stamped = { reported: { host: "laptop", os_user: "x" }, authenticated: { user: "atin", machine: "Hyperion" } };
  assert.equal(logic.originLabel("agent:qa", stamped), "atin@Hyperion");
  assert.equal(logic.originLabel("agent:qa", { user: "atin", machine: "Atlas" }), "atin@Atlas");
  assert.equal(logic.originLabel("agent:qa", { op: "issue.file" }), "");
  assert.equal(logic.originLabel("agent:qa", null), "");
  assert.notEqual(logic.originLabel("agent:qa", raw), "unknown@unknown");
});

test("a row is labelled by the issue's own state, whatever list it sits in", () => {
  const actor = "agent:qa";
  const rows = {
    open: { id: "1", state: "open", filed_by: actor, matched_by: "filed" },
    linked: { id: "2", state: "linked", filed_by: actor, matched_by: "filed", tasks: [{ short_id: "LAT-9", status: "in_progress" }] },
    resolved: { id: "3", state: "resolved", filed_by: actor, matched_by: "filed", tasks: [{ short_id: "LAT-4", status: "done" }, { short_id: "LAT-5", status: "done" }] },
    dismissed: { id: "4", state: "dismissed", filed_by: "human:atin", matched_by: "commented", closure: { kind: "dismissed", reason: "nope" } },
    duplicate: { id: "5", state: "duplicate", filed_by: actor, matched_by: "filed", closure: { kind: "duplicate", duplicate_of: "LAT-I2" } },
  };
  assert.deepEqual(logic.rowTag(rows.open, actor), { queue: "open", commented: false, kind: "actor", actor });
  assert.equal(logic.rowTag(rows.linked, actor).kind, "task");
  assert.equal(logic.rowTag(rows.linked, actor).task, "LAT-9");
  assert.equal(logic.rowTag(rows.linked, actor).status, "in progress");
  assert.equal(logic.rowTag(rows.resolved, actor).more, 1);
  assert.deepEqual(logic.rowTag(rows.dismissed, actor), { queue: "closed", commented: true, kind: "dismissed" });
  assert.deepEqual(logic.rowTag(rows.duplicate, actor), { queue: "closed", commented: false, kind: "duplicate", of: "LAT-I2" });
  // Out of the person view nothing reads "commented".
  assert.equal(logic.rowTag(rows.dismissed, null).commented, false);
});

test("video frame sample times match the issue media contract and survive an unknown duration", () => {
  assert.deepEqual(logic.frameTimes(999), [0]);
  assert.deepEqual(logic.frameTimes(10_000), [0, 1980, 3960, 5940, 7920, 9900]);
  assert.equal(logic.frameTimes(60_000).length, 8);
  for (const unknown of [Infinity, -Infinity, NaN, 0, -5, null, undefined]) {
    const times = logic.frameTimes(unknown);
    assert.deepEqual(times, [0], String(unknown));
    assert.ok(times.every(Number.isFinite));
  }
  assert.equal(logic.durationMs(Infinity), null);
  assert.equal(logic.durationMs(NaN), null);
  assert.equal(logic.durationMs(0), null);
  assert.equal(logic.durationMs(7.25), 7250);
  assert.equal(JSON.stringify({ duration_ms: logic.durationMs(Infinity) }), '{"duration_ms":null}');
});

test("a video is pictured by its first stored frame, never by the video file", () => {
  const video = { kind: "video", url: "/m/clip.mp4", frames: [{ url: "/m/f0.jpg" }, { url: "/m/f1.jpg" }] };
  const photo = { kind: "photo", url: "/m/shot.png" };
  assert.equal(logic.stillOf(video), "/m/f0.jpg");
  assert.equal(logic.stillOf(photo), "/m/shot.png");
  assert.equal(logic.stillOf({ kind: "video", url: "/m/clip.mp4", frames: [] }), null);
  assert.equal(logic.stillOf({ kind: "video", url: "/m/clip.mp4", frames: [{ url: null }] }), null);
  assert.equal(logic.rowMedia([photo, video]), video);
  assert.equal(logic.rowMedia([photo]), photo);
});

test("formatting follows the prototype", () => {
  const now = Date.parse("2026-10-02T12:00:00Z");
  assert.equal(logic.relativeTime("2026-10-02T11:59:30Z", now), "just now");
  assert.equal(logic.relativeTime("2026-10-02T11:57:00Z", now), "3m ago");
  assert.equal(logic.relativeTime("2026-10-02T09:00:00Z", now), "3h ago");
  assert.equal(logic.relativeTime("2026-09-28T12:00:00Z", now), "4d ago");
  assert.equal(logic.formatDuration(25), "0:25");
  assert.equal(logic.formatDuration(7.5), "0:07");
  assert.equal(logic.formatDuration(9.9, true), "0:09.9");
  assert.equal(logic.formatDuration(null), "0:00");
  assert.equal(logic.formatBytes(94 * 1024), "94 KB");
  assert.equal(logic.trayTotal(0, logic.DEFAULT_LIMITS), "1 KB of 250 MB");
  assert.equal(logic.trayTotal(3 * 1024 * 1024, logic.DEFAULT_LIMITS), "3.0 MB of 250 MB");
});

test("filing refuses non-media and oversize files, and explains a missing title", () => {
  const limits = logic.mediaLimits({ issues: { max_media_mb: 1, max_issue_media_mb: 2 } });
  assert.deepEqual(limits, { file: 1024 * 1024, issue: 2 * 1024 * 1024 });
  assert.deepEqual(logic.mediaLimits({}), logic.DEFAULT_LIMITS);
  assert.equal(logic.kindOf({ type: "image/png", name: "a.png" }), "photo");
  assert.equal(logic.kindOf({ type: "", name: "clip.MOV" }), "video");
  assert.equal(logic.kindOf({ type: "application/pdf", name: "a.pdf" }), null);
  assert.equal(logic.mediaProblem(0, { kind: null, bytes: 10 }, limits), "Only photos and videos can be attached.");
  assert.match(logic.mediaProblem(0, { kind: "photo", bytes: 2 * 1024 * 1024 }, limits), /^Too large: 2 MB\. The limit is 1 MB a file\.$/);
  assert.match(logic.mediaProblem(1.5 * 1024 * 1024, { kind: "photo", bytes: 0.9 * 1024 * 1024 }, limits), /^Too much in all/);
  assert.equal(logic.mediaProblem(0, { kind: "video", bytes: 10 }, limits), null);
  assert.equal(logic.filingProblem("", { reading: 0, accepted: 0 }), "An issue needs a title.");
  assert.match(logic.filingProblem(" ", { reading: 0, accepted: 1, firstKind: "photo" }), /^Add a title saying what the photo shows\./);
  assert.equal(logic.filingProblem("x", { reading: 2, accepted: 0 }), "Still reading 2 files. A moment.");
  assert.equal(logic.filingProblem("Title", { reading: 0, accepted: 3 }), null);
});

test("LOCAL_ONLY means issues are not available here; other errors are load errors", () => {
  assert.equal(logic.isUnavailable({ code: "LOCAL_ONLY", message: "Issue reads are local-only." }), true);
  assert.equal(logic.isUnavailable({ code: "NOT_FOUND" }), false);
  assert.equal(logic.isUnavailable(new Error("API error")), false);
  assert.equal(logic.isUnavailable(null), false);
});

test("refresh plan: unchanged or unrelated rows leave the selected issue alone", () => {
  const previous = [
    { id: "a", seq: 1, title: "A", comment_count: 0 },
    { id: "b", seq: 2, title: "B", comment_count: 0 },
  ];
  const same = logic.planRefresh({ previousRows: previous, nextRows: JSON.parse(JSON.stringify(previous)), selectedId: "a", selectedIndex: 0 });
  assert.deepEqual(same, { list: false, changedIds: [], cursor: { id: "a", index: 0 }, detail: "keep" });

  const newIssue = logic.planRefresh({ previousRows: previous, nextRows: previous.concat({ id: "c", seq: 3, title: "C" }), selectedId: "a", selectedIndex: 0 });
  assert.equal(newIssue.list, true);
  assert.equal(newIssue.detail, "keep");

  const otherComment = logic.planRefresh({
    previousRows: previous,
    nextRows: [previous[0], Object.assign({}, previous[1], { comment_count: 1 })],
    selectedId: "a", selectedIndex: 0,
  });
  assert.deepEqual(otherComment.changedIds, ["b"]);
  assert.equal(otherComment.detail, "keep");

  const ownComment = logic.planRefresh({
    previousRows: previous,
    nextRows: [Object.assign({}, previous[0], { comment_count: 1 }), previous[1]],
    selectedId: "a", selectedIndex: 0,
  });
  assert.equal(ownComment.detail, "reload");
});

test("refresh plan: a selected issue that leaves the queue stays selected; only one gone from the board lets go", () => {
  const previous = [{ id: "a", seq: 1 }, { id: "b", seq: 2 }, { id: "c", seq: 3 }];
  const next = [{ id: "a", seq: 1 }, { id: "c", seq: 3 }];
  const kept = logic.planRefresh({ previousRows: previous, nextRows: next, selectedId: "b", selectedIndex: 1, selectedSlot: 1, selectedExists: true });
  assert.deepEqual(kept.cursor, { id: "b", index: 1 }, "still on b, at the slot where it would sit");
  assert.equal(kept.detail, "reload", "shown, updated in place");
  const gone = logic.planRefresh({ previousRows: previous, nextRows: next, selectedId: "b", selectedIndex: 1, selectedExists: false });
  assert.deepEqual(gone.cursor, { id: "c", index: 1 });
  assert.equal(gone.detail, "switch");
  const empty = logic.planRefresh({ previousRows: [], nextRows: [], selectedId: null, selectedIndex: 0 });
  assert.deepEqual(empty, { list: false, changedIds: [], cursor: { id: null, index: 0 }, detail: "keep" });
});

test("j and k from an issue that left the queue start where it would sit", () => {
  const bySeq = (x, y) => x.seq - y.seq;
  const rows = [{ id: "a", seq: 1 }, { id: "c", seq: 3 }, { id: "d", seq: 4 }];
  assert.equal(logic.slotOf(rows, { id: "b", seq: 2 }, bySeq), 1);
  assert.equal(logic.slotOf(rows, { id: "e", seq: 9 }, bySeq), 3);
  assert.equal(logic.stepIndex(3, -1, 1, 1), 1, "j: the row now in its slot (c)");
  assert.equal(logic.stepIndex(3, -1, 1, -1), 0, "k: the row before it (a)");
  assert.equal(logic.stepIndex(3, -1, 3, 1), 2, "past the end: the last row");
  assert.equal(logic.stepIndex(3, -1, 0, -1), 0, "before the start: the first row");
  assert.equal(logic.stepIndex(3, 1, 0, 1), 2, "on a row: one step");
  assert.equal(logic.stepIndex(0, -1, 0, 1), -1);
});

test("history folds a filing's own media into the filed line, as the prototype words it", () => {
  const op = (id) => ({ op: "issue.file", op_id: id });
  const filed = (id) => ({ type: "issue_filed", origin: op(id), data: {} });
  const media = (id, kind) => ({ type: "issue_media_added", origin: op(id), data: { kind } });
  const line = (events) => logic.historyEntries(events).map((e) => e.type + (e.filedMedia ? " " + logic.mediaCountText(e.filedMedia.photos, e.filedMedia.videos) : ""));
  assert.deepEqual(line([filed("a"), media("a", "video")]), ["issue_filed 1 video"]);
  assert.deepEqual(line([filed("a"), media("a", "photo"), media("a", "photo")]), ["issue_filed 2 photos"]);
  assert.deepEqual(line([filed("a"), media("a", "photo"), media("a", "video")]), ["issue_filed 1 photo and 1 video"]);
  assert.deepEqual(line([filed("a"), media("b", "photo")]), ["issue_filed", "issue_media_added"], "another operation keeps its line");
  assert.deepEqual(line([{ type: "issue_filed", data: {} }, { type: "issue_media_added", data: { kind: "photo" } }]),
    ["issue_filed", "issue_media_added"], "without an op_id nothing is folded");
  assert.deepEqual(logic.historyEntries(undefined), []);
});

test("the media key ignores everything but which media and frames are shown", () => {
  const media = [{ id: "m1", url: "/u", frames: [{ url: "/f0" }], width: 640 }];
  assert.equal(logic.mediaKey("i", media), logic.mediaKey("i", [Object.assign({}, media[0], { width: 641 })]));
  assert.notEqual(logic.mediaKey("i", media), logic.mediaKey("i", media.concat({ id: "m2", url: "/v" })));
});

test("triageAction offers Close for live issues and Reopen for closed ones", () => {
  for (const state of ["open", "linked", "resolved"]) assert.equal(logic.triageAction({ state }), "close", state);
  for (const state of ["dismissed", "duplicate"]) assert.equal(logic.triageAction({ state }), "reopen", state);
  assert.equal(logic.triageAction({}), "close", "an issue with no state is open");
  assert.equal(logic.triageAction(null), null);
  assert.equal(logic.triageAction(undefined), null);
});
