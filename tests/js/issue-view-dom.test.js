"use strict";

// Drives the real issue-view.js against a small DOM (support/mini-dom.js) and a
// fake issue API. These pin the behaviour the reviewers and Atin checked by hand:
// a refresh never interrupts playback, scroll, focus or drafts; person-page labels;
// history origins; video stills; the unavailable state; the filing panel's keys,
// paste and drop. ISSUE_VIEW_FILE=<path> (and ISSUE_VIEW_LOGIC_FILE=<path>) run them
// against another copy of the view, e.g. an old one, to show a test goes red without its fix.

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { createWindow, install, Event } = require("./support/mini-dom.js");
const { esc } = require("../../src/lattice/dashboard/static/escape.js");

const STATIC = path.join(__dirname, "..", "..", "src", "lattice", "dashboard", "static");
const LOGIC_FILE = process.env.ISSUE_VIEW_LOGIC_FILE ? path.resolve(process.env.ISSUE_VIEW_LOGIC_FILE) : path.join(STATIC, "issue-view-logic.js");
const VIEW_FILE = process.env.ISSUE_VIEW_FILE ? path.resolve(process.env.ISSUE_VIEW_FILE) : path.join(STATIC, "issue-view.js");

const NOW = Date.now();
const ago = (minutes) => new Date(NOW - minutes * 60000).toISOString();
const ATLAS = { op: "issue.file", op_id: "op_1", reported: { host: "Atlas", os_user: "atin" } };

function clone(value) { return JSON.parse(JSON.stringify(value)); }

// A fake issue API holding full issue details; the list answers summaries.
function makeServer(details) {
  const server = {
    details,
    error: null,
    calls: [],
    add(issue) { server.details.push(issue); },
    find(shortId) { return server.details.find((issue) => issue.short_id === shortId); },
    comment(shortId, author, body) {
      server.find(shortId).comments.push({ author, body, created_at: new Date().toISOString(), origin: { user: "atin", machine: "Atlas" } });
    },
    async get(url) {
      server.calls.push(url);
      await null;
      if (server.error) { const error = new Error(server.error.message || "failed"); error.code = server.error.code; throw error; }
      const summary = (issue) => {
        const row = clone(issue);
        row.comment_count = issue.comments.length;
        delete row.comments;
        delete row.events;
        return row;
      };
      if (url === "/api/issues") return server.details.map(summary);
      if (url.startsWith("/api/issues?by=")) {
        const actor = decodeURIComponent(url.slice("/api/issues?by=".length));
        return server.details.filter((issue) => issue.filed_by === actor || issue.comments.some((c) => c.author === actor)).map((issue) => {
          const mine = issue.comments.filter((c) => c.author === actor);
          return Object.assign(summary(issue), {
            matched_by: issue.filed_by === actor ? "filed" : "commented",
            actor_comment_count: mine.length,
            actor_comment_origins: mine.map((c) => c.origin),
            actor_activity_at: [issue.filed_by === actor ? issue.filed_at : ""].concat(mine.map((c) => c.created_at)).sort().pop(),
          });
        });
      }
      const id = decodeURIComponent(url.slice("/api/issues/".length));
      const issue = server.details.find((item) => item.id === id);
      if (!issue) throw new Error("not found");
      return clone(issue);
    },
  };
  return server;
}

function issue(seq, fields) {
  return Object.assign({
    id: "iss_" + seq, short_id: "T-I" + seq, seq, title: "Issue " + seq, description: "", state: "open",
    filed_by: "agent:qa", filed_at: ago(60 - seq), filed_origin: { user: "atin", machine: "Atlas" },
    media: [], tasks: [], closure: null, comments: [], events: [],
  }, fields || {});
}

const VIDEO = {
  id: "med_v", kind: "video", url: "/api/issues/iss_1/media/med_v", width: 640, height: 360, duration_ms: 25000,
  frames: [{ t_ms: 0, url: "/api/issues/iss_1/media/med_v/frames/t0000.000s.jpg" }, { t_ms: 9900, url: "/api/issues/iss_1/media/med_v/frames/t0009.900s.jpg" }],
};

class FakeImage {
  set src(value) { this._src = value; setImmediate(() => this.onload && this.onload()); }
  get src() { return this._src; }
}

async function flush(rounds = 6) {
  for (let i = 0; i < rounds; i++) await new Promise((resolve) => setImmediate(resolve));
}

// Wait for a condition driven by real async work (file reads, hashing) rather than
// guess a number of event-loop turns: a slow runner needs more of them.
async function until(condition, what, timeoutMs = 5000) {
  const deadline = Date.now() + timeoutMs;
  while (!condition()) {
    if (Date.now() > deadline) assert.fail(`timed out waiting for ${what}`);
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

function boot(server, extra) {
  const win = createWindow();
  const restore = install(win);
  globalThis.apiUrl = (_base, p) => p;
  globalThis.Image = FakeImage;
  delete globalThis.IssueViewLogic;
  delete globalThis.IssueDashboard;
  vm.runInThisContext(fs.readFileSync(LOGIC_FILE, "utf8"), { filename: LOGIC_FILE });
  vm.runInThisContext(fs.readFileSync(VIEW_FILE, "utf8"), { filename: VIEW_FILE });
  document.body.innerHTML = '<div class="nav"><span class="nav-tab" data-view="issues"><span class="nav-tab-label">Issues</span>' +
    '<span class="issue-nav-count" id="issue-nav-count"></span></span></div><div class="content" id="app"></div>' + ((extra && extra.html) || "");
  const posts = [];
  const toasts = [];
  const dashboard = globalThis.IssueDashboard.mount({
    app: document.getElementById("app"),
    api: (url) => server.get(url),
    apiPost: (url, body) => {
      posts.push({ url, body });
      return extra && extra.apiPost ? extra.apiPost(url, body) : Promise.resolve({ id: "iss_99", short_id: "T-I99" });
    },
    esc, hosted: !!(extra && extra.hosted), readOnly: !!(extra && extra.readOnly), basePath: "/", showToast: (message) => toasts.push(message),
    uploadIssueMedia: extra && extra.uploadIssueMedia,
  });
  return {
    dashboard, posts, toasts,
    done() {
      dashboard.destroy();
      restore();
      delete globalThis.apiUrl;
      delete globalThis.Image;
    },
  };
}

function key(target, name, init) {
  const event = new Event("keydown", Object.assign({ bubbles: true, cancelable: true, key: name }, init || {}));
  target.dispatchEvent(event);
  return event;
}
// DOM nodes are compared by identity only: a failing deep diff of a node graph never finishes.
function same(actual, expected, message) { assert.ok(actual === expected, message || "not the same node"); }
function none(actual, message) { assert.ok(actual == null, message || "expected nothing"); }
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => document.querySelectorAll(selector);

test("a refresh never interrupts a playing video, the scroll position, focus or a half-typed comment", async () => {
  const server = makeServer([
    issue(1, { title: "Drag to Done snaps back", media: [VIDEO], comments: [{ author: "human:atin", body: "seen", created_at: ago(5), origin: { user: "atin", machine: "Atlas" } }] }),
    issue(2, { media: [{ id: "med_p", kind: "photo", url: "/api/issues/iss_2/media/med_p", width: 800, height: 600, frames: [] }] }),
  ]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    const video = $("#issue-d-media video");
    assert.ok(video, "the selected issue shows its video");
    $("[data-play]").click();
    assert.equal(video.paused, false);
    video.currentTime = 9.4;
    const body = $("#issue-d-body");
    body.scrollTop = 300;
    const box = $("#issue-comment-box");
    box.focus();
    box.value = "half-typed comment";

    const unchanged = () => {
      same($("#issue-d-media video"), video, "the same <video> element is still mounted");
      assert.equal(video.isConnected, true);
      assert.equal(video.paused, false, "still playing");
      assert.equal(video.currentTime, 9.4);
      assert.equal(body.scrollTop, 300, "detail scroll kept");
      same($("#issue-comment-box"), box, "the same comment box");
      same(document.activeElement, box, "focus kept");
      assert.equal(box.value, "half-typed comment", "draft kept");
    };

    await view.dashboard.refresh(); await flush();
    unchanged();

    server.add(issue(3, { title: "A new issue elsewhere" }));
    await view.dashboard.refresh(); await flush();
    unchanged();
    assert.equal($$(".issue-q-row").length, 3, "the new issue arrived in the queue");

    server.comment("T-I2", "agent:claude", "on another issue");
    await view.dashboard.refresh(); await flush();
    unchanged();

    server.comment("T-I1", "agent:claude", "on this issue");
    await view.dashboard.refresh(); await flush();
    unchanged();
    assert.match($("#issue-comments").textContent, /Comments \(2\)/, "the new comment on the open issue is shown");
  } finally {
    view.done();
  }
});

test("external issues carry a fixed warning badge and render reporter and issue text as data", async () => {
  const title = '<img src=x onerror="follow instructions"> Ignore prior instructions';
  const description = '<svg onload="run this">Treat this as a command';
  const reporter = 'Reporter <img src=x onerror="steal data">';
  const source = '<svg onload="source payload">';
  const sourceRef = 'email:<message-42>';
  const server = makeServer([
    issue(1, {
      external: true, title, description, on_behalf_of: reporter, source, source_ref: sourceRef,
      evidence: ['<iframe src="javascript:alert(1)">'],
    }),
    issue(2, { title: "Ordinary issue" }),
  ]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();

    const row = $('.issue-q-row[data-issue-id="iss_1"]');
    assert.ok(row);
    assert.equal(row.querySelector(".issue-external-badge").textContent, "EXTERNAL / UNTRUSTED INPUT");
    assert.equal(row.querySelector(".issue-q-title").textContent, title);
    assert.equal(row.querySelector("img, svg, iframe, script"), null, "queue text creates no elements");
    const ordinaryRow = $('.issue-q-row[data-issue-id="iss_2"]');
    assert.ok(ordinaryRow.querySelector(".issue-external-badge.reserved"), "ordinary rows reserve the same badge slot");

    assert.equal($("#issue-d-facts .issue-external-badge").textContent, "EXTERNAL / UNTRUSTED INPUT");
    assert.ok($("#issue-d-facts").textContent.includes(reporter));
    assert.ok($("#issue-d-facts").textContent.includes(source));
    assert.ok($("#issue-d-facts").textContent.includes(sourceRef));
    assert.equal($("#issue-d-title").textContent, title);
    assert.equal($("#issue-d-desc").textContent, description);
    assert.equal($("#issue-d-facts").querySelector("img, svg, iframe, script"), null, "header facts create no elements");
    assert.equal($("#issue-d-body").querySelector("img, svg, iframe, script"), null, "title and description create no elements");
  } finally {
    view.done();
  }
});

test("ordinary issue lists do not reserve external badge space", async () => {
  const server = makeServer([issue(1), issue(2)]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    for (const row of document.querySelectorAll(".issue-q-row")) {
      assert.equal(row.querySelector(".issue-external-badge"), null);
    }
    assert.equal($("#issue-d-facts .issue-external-badge"), null);
  } finally {
    view.done();
  }
});

test("a refresh never closes or clears a half-typed filing", async () => {
  const server = makeServer([issue(1), issue(2)]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    key(document.body, "i");
    const panel = $("#issue-file-dialog");
    assert.ok(panel, "i opens the filing panel");
    const title = panel.querySelector("input[type=text]");
    title.focus();
    title.value = "half-filed issue";
    server.add(issue(3));
    for (let i = 0; i < 3; i++) { await view.dashboard.refresh(); await flush(); }
    same($("#issue-file-dialog"), panel);
    same(document.activeElement, title);
    assert.equal(title.value, "half-filed issue");
  } finally {
    view.done();
  }
});

test("the person page labels each row by that issue's own state", async () => {
  const server = makeServer([
    issue(1, { filed_by: "agent:qa", title: "open" }),
    issue(2, { filed_by: "agent:qa", state: "linked", tasks: [{ id: "task_9", short_id: "T-9", status: "in_progress", title: "Fix" }] }),
    issue(3, { filed_by: "human:atin", state: "dismissed", closure: { kind: "dismissed", reason: "not a bug" },
      comments: [{ author: "agent:qa", body: "cannot reproduce", created_at: ago(1), origin: { user: "atin", machine: "Hyperion" } }] }),
    issue(4, { filed_by: "agent:qa", state: "duplicate", closure: { kind: "duplicate", duplicate_of: "iss_1" } }), // the server names the target by its id
  ]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    $('.issue-q-row [data-actor="agent:qa"]').click();
    await flush();
    const tags = {};
    $$(".issue-q-row").forEach((row) => { tags[row.getAttribute("data-issue-id")] = row.querySelector(".issue-q-tag").textContent; });
    assert.deepEqual(tags, {
      iss_1: "agent:qa",
      iss_2: "T-9 in progress",
      iss_3: "commented · dismissed",
      iss_4: "duplicate of T-I1",
    });
    const head = $("#issue-person-head");
    assert.ok(head.querySelector('.issue-actor-agent[data-actor="agent:qa"]'), "the head shows the person's actor chip");
    assert.match(head.textContent, /3 filed · 1 comment/);
  } finally {
    view.done();
  }
});

test("history lines name the event's reported user and machine", async () => {
  const server = makeServer([issue(1, {
    events: [
      { type: "issue_filed", actor: "agent:qa", ts: ago(10), origin: ATLAS, data: {} },
      { type: "issue_comment_added", actor: "human:atin", ts: ago(5), origin: { op: "issue.comment", reported: { host: "Hyperion", os_user: "atin" } }, data: {} },
    ],
  })]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    $(".issue-history-toggle").click();
    const lines = $$(".issue-history-what").map((line) => line.textContent);
    assert.deepEqual(lines, ["agent:qa · atin@Atlas filed", "human:atin · Hyperion commented"]);
    assert.ok(!$("#issue-history").textContent.includes("unknown@unknown"));
  } finally {
    view.done();
  }
});

test("issue history uses linked task short IDs and safely falls back when no task row matches", async () => {
  const taskId = "task_01ARZ3NDEKTSV4RRFFQ69G5FAV";
  const unlinkedTaskId = "task_01ARZ3NDEKTSV4RRFFQ69G5FAW";
  const unknown = "task_<svg onload=alert(1)>";
  const server = makeServer([issue(1, {
    tasks: [{ id: taskId, short_id: "T-9", status: "backlog", title: "Fix it" }],
    task_short_ids: { [unlinkedTaskId]: "T-10" },
    events: [
      { id: "ev_1", type: "issue_linked", actor: "human:atin", ts: ago(10), data: { task_id: taskId } },
      { id: "ev_2", type: "issue_unlinked", actor: "human:atin", ts: ago(7), data: { task_id: unlinkedTaskId } },
      { id: "ev_3", type: "issue_unlinked", actor: "human:atin", ts: ago(5), data: { task_id: unknown } },
    ],
  })]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    $(".issue-history-toggle").click();
    const history = $("#issue-history");
    assert.match(history.textContent, /linked to T-9/);
    assert.match(history.textContent, /unlinked from T-10/);
    assert.match(history.textContent, /unlinked from task_<svg onload=alert\(1\)>/);
    assert.equal(history.querySelector("svg"), null, "the fallback is escaped as text");
  } finally {
    view.done();
  }
});

test("hosted issue view loads details and submits media through the session staging hook", async () => {
  const server = makeServer([issue(1)]);
  const uploads = [];
  const view = boot(server, {
    hosted: true,
    uploadIssueMedia: async (file) => {
      uploads.push(file.name);
      return { payload: { filename: file.name, sha256: "a".repeat(64), size: file.size, staged: true } };
    },
  });
  try {
    view.dashboard.render();
    await flush();
    assert.equal(shownIssue(), "T-I1");
    assert.ok(server.calls.includes("/api/issues/iss_1"), "hosted detail is fetched");
    assert.match($(".issue-q-foot").textContent, /i file/, "hosted issue filing advertises its keyboard shortcut");

    key(document.body, "i");
    assert.ok($("#issue-file-dialog"), "hosted `i` opens the filing panel");
    key(document.body, "Escape");
    none($("#issue-file-dialog"));

    const file = new File([new Uint8Array([137, 80, 78, 71, 1])], "Screenshot.png", { type: "image/png" });
    const paste = new Event("paste", { bubbles: true, cancelable: true,
      clipboardData: { items: [{ kind: "file", getAsFile: () => file }], types: ["Files"] } });
    document.body.dispatchEvent(paste);
    assert.ok($("#issue-file-dialog"), "hosted sessions can open the issue filing panel");
    await flush();
    $("#issue-fi-text").value = "Hosted screenshot";
    key($("#issue-fi-text"), "Enter", { ctrlKey: true });
    await until(() => view.posts.length > 0, "the hosted issue filing");
    assert.deepEqual(uploads, ["Screenshot.png"]);
    assert.equal(view.posts[0].body.media[0].payload.staged, true);
    assert.equal("content_b64" in view.posts[0].body.media[0].payload, false);
  } finally {
    view.done();
  }
});

test("a bound checkout keeps mirrored issues visible and hides every issue write", async () => {
  const server = makeServer([issue(1)]);
  const view = boot(server, { readOnly: true });
  try {
    view.dashboard.render();
    await flush();
    assert.equal($$(".issue-q-row").length, 1, "the mirrored issue remains visible");
    assert.match($(".issue-readonly-note").textContent, /bound checkout is read-only/);
    assert.match($(".issue-readonly-note").textContent, /hosted dashboard or with lattice issue/);
    for (const selector of ["#issue-triage", "#issue-close-pop", "#issue-comment-box", "#issue-comment-post"]) {
      assert.equal($(selector), null, selector + " is absent");
    }
    assert.doesNotMatch($(".issue-q-foot").textContent, /i file/);

    key(document.body, "i");
    document.body.dispatchEvent(new Event("dragenter", { bubbles: true, cancelable: true, dataTransfer: { types: ["Files"] } }));
    assert.equal($("#issue-file-dialog"), null);
    assert.equal($(".issue-drop-hint"), null);
    assert.equal(view.posts.length, 0);
    assert.equal(view.toasts.length, 0, "the visible mirror has no unavailable-write toast");
  } finally {
    view.done();
  }
});

test("the compact nav keeps width slack for a three-digit Issues count", () => {
  const css = fs.readFileSync(path.join(STATIC, "issue-view.css"), "utf8");
  const compact = css.match(/@media\s*\(max-width:\s*1599px\)\s*\{([\s\S]*?)\n\}/);
  assert.ok(compact, "compact navbar rule exists");
  assert.match(compact[1], /\.nav \.nav-tab\s*\{[^}]*padding-left:\.375rem;\s*padding-right:\.375rem/);
});

test("a filing with media is one history line; media added later by someone else keeps its own", async () => {
  const later = { op: "issue.attach", op_id: "op_2", reported: { host: "Hyperion", os_user: "atin" } };
  const server = makeServer([issue(1, {
    events: [
      { type: "issue_filed", actor: "agent:qa", ts: ago(10), origin: ATLAS, data: {} },
      { type: "issue_media_added", actor: "agent:qa", ts: ago(10), origin: ATLAS, data: { kind: "photo" } },
      { type: "issue_media_added", actor: "agent:qa", ts: ago(10), origin: ATLAS, data: { kind: "video" } },
      { type: "issue_media_added", actor: "human:atin", ts: ago(3), origin: later, data: { kind: "photo" } },
    ],
  })]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    assert.equal($(".issue-history-toggle").textContent, "▸History (2)");
    $(".issue-history-toggle").click();
    assert.deepEqual($$(".issue-history-what").map((line) => line.textContent), [
      "agent:qa · atin@Atlas filed with 1 photo and 1 video",
      "human:atin · Hyperion added a photo",
    ]);
  } finally {
    view.done();
  }
});

test("a video is pictured by its first frame in the queue and as the inline poster, never by the video file", async () => {
  const noFrames = Object.assign({}, VIDEO, { id: "med_w", url: "/api/issues/iss_2/media/med_w", frames: [] });
  const server = makeServer([issue(1, { media: [VIDEO] }), issue(2, { media: [noFrames] })]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    const rows = $$(".issue-q-row");
    assert.equal(rows[0].querySelector(".issue-q-thumb img").getAttribute("src"), VIDEO.frames[0].url);
    assert.equal(rows[0].querySelector(".issue-q-video").textContent, "▶ 0:25");
    none(rows[1].querySelector(".issue-q-thumb img"), "no frames: the dark slot without a picture");
    assert.ok(!$$(".issue-q-thumb img").some((img) => /med_[vw]$/.test(img.getAttribute("src"))));
    assert.equal($("#issue-d-media video").getAttribute("poster"), VIDEO.frames[0].url);
    assert.equal($("#issue-nav-count").textContent, "2", "the Issues tab counts open issues");
  } finally {
    view.done();
  }
});

test("a LOCAL_ONLY answer shows the not-available state, not a load error", async () => {
  const server = makeServer([issue(1)]);
  server.error = { code: "LOCAL_ONLY", message: "Issue reads are local-only." };
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    assert.match($("#app").textContent, /Issues are not available on this board yet\./);
    assert.ok(!$("#app").textContent.includes("Could not load"));
    assert.equal(document.body.classList.contains("issue-view-active"), false);
  } finally {
    view.done();
  }
});

test("filing panel: prototype copy, Enter moves to the description, an empty title is explained, Cmd+Enter files", async () => {
  const server = makeServer([issue(1)]);
  const view = boot(server);
  try {
    key(document.body, "i"); // from any view
    const panel = $("#issue-file-dialog");
    assert.ok(panel);
    assert.equal(panel.querySelector(".issue-dialog-head button").textContent, "Esc");
    const title = $("#issue-fi-text");
    const description = $("#issue-fi-desc");
    assert.equal(title.getAttribute("placeholder"), "Footer overlaps the Complete button at 400px");
    assert.equal(description.getAttribute("placeholder"), "Steps, what you expected, what happened");
    assert.match(panel.textContent, /Photos and video \(optional\)/);
    assert.match($("#issue-fi-total").textContent, / of 250 MB$/);
    assert.match($("#issue-fi-tray").textContent, /Paste, drop or choose a photo or video/);
    assert.equal(panel.querySelector(".issue-fi-choose").textContent, "Choose a file");
    assert.equal(panel.querySelector(".issue-hint").textContent, "⌘ V pastes a screenshot. ⌘ Enter files it. No story is made.");
    assert.ok(panel.querySelector("[data-file-action=submit]").classList.contains("btn-primary"));
    same(document.activeElement, title);

    key(title, "Enter");
    same(document.activeElement, description, "Enter in the title moves to the description");

    key(description, "Enter", { metaKey: true });
    await flush();
    assert.equal($("#issue-fi-err").textContent, "An issue needs a title.");
    assert.equal(view.posts.length, 0);

    title.value = "Lane header wraps";
    description.value = "At 400px";
    key(description, "Enter", { metaKey: true });
    await flush();
    assert.deepEqual(view.posts, [{ url: "/api/issues", body: { title: "Lane header wraps", description: "At 400px", media: [] } }]);
    none($("#issue-file-dialog"), "filed: the panel closes");
    assert.deepEqual(view.toasts, ["Filed T-I99: Lane header wraps"]);

    key(document.body, "i");
    assert.ok($("#issue-file-dialog"));
    key(document.body, "Escape");
    none($("#issue-file-dialog"), "Esc closes it");
  } finally {
    view.done();
  }
});

test("pasting or dropping a file anywhere opens the panel with it attached", async () => {
  const server = makeServer([issue(1)]);
  const view = boot(server);
  try {
    const png = new File([new Uint8Array([137, 80, 78, 71, 1])], "Screenshot.png", { type: "image/png" });
    const paste = new Event("paste", { bubbles: true, cancelable: true,
      clipboardData: { items: [{ kind: "file", getAsFile: () => png }], types: ["Files"] } });
    document.body.dispatchEvent(paste);
    assert.equal(paste.defaultPrevented, true);
    assert.ok($("#issue-file-dialog"), "paste opens the panel");
    await flush();
    assert.equal($(".issue-tile-kind").textContent, "photo");
    assert.equal($(".issue-tile-size").textContent, "1 KB");

    const pdf = new File(["%PDF"], "notes.pdf", { type: "application/pdf" });
    const intoPanel = new Event("drop", { bubbles: true, cancelable: true, dataTransfer: { types: ["Files"], files: [pdf] } });
    $("#issue-fi-tray").dispatchEvent(intoPanel);
    assert.equal($(".issue-tile-refused .issue-tile-why").textContent, "Only photos and videos can be attached.");

    $("#issue-fi-text").value = "Footer overlaps";
    key($("#issue-fi-text"), "Enter", { ctrlKey: true });
    await until(() => view.posts.length > 0, "the filing to be posted");
    assert.equal(view.posts.length, 1);
    const media = view.posts[0].body.media;
    assert.equal(media.length, 1, "the refused PDF is not sent");
    assert.equal(media[0].payload.filename, "Screenshot.png");
    assert.equal(media[0].payload.content_b64, Buffer.from([137, 80, 78, 71, 1]).toString("base64"));
    assert.match(media[0].payload.sha256, /^[0-9a-f]{64}$/);

    const enter = new Event("dragenter", { bubbles: true, cancelable: true, dataTransfer: { types: ["Files"] } });
    document.body.dispatchEvent(enter);
    assert.equal($(".issue-drop-label").textContent, "Drop to file a new issue with this attached");
    assert.ok(document.body.classList.contains("issue-dragging-files"));
    const drop = new Event("drop", { bubbles: true, cancelable: true, dataTransfer: { types: ["Files"], files: [png] } });
    document.body.dispatchEvent(drop);
    assert.equal(document.body.classList.contains("issue-dragging-files"), false);
    assert.ok($("#issue-file-dialog"), "drop opens the panel");
    assert.equal($$(".issue-tile").length, 1);
  } finally {
    view.done();
  }
});

test("the drop hint sits below the nav's real bottom, even when the nav wraps to more rows", async () => {
  const server = makeServer([issue(1)]);
  const view = boot(server);
  try {
    const nav = $(".nav");
    nav.offsetHeight = 42; // one row
    const drag = () => document.body.dispatchEvent(new Event("dragenter", { bubbles: true, cancelable: true, dataTransfer: { types: ["Files"] } }));
    drag();
    assert.equal($(".issue-drop-hint").style.top, "60px");
    document.body.dispatchEvent(new Event("drop", { bubbles: true, cancelable: true, dataTransfer: { types: ["Files"], files: [] } }));
    nav.offsetHeight = 122; // wrapped to three rows
    drag();
    assert.equal($(".issue-drop-hint").style.top, "140px", "below the third nav row, not over it");
  } finally {
    view.done();
  }
});

test("Inbox keys stay out of the way while a dashboard drawer is open", async () => {
  const server = makeServer([issue(1), issue(2)]);
  const view = boot(server, { html: '<div class="filter-panel" id="filter-panel"></div>' });
  try {
    view.dashboard.render();
    await flush();
    const selected = () => $(".issue-q-row.cursor .issue-iid").textContent;
    assert.equal(selected(), "T-I1");
    key(document.body, "j");
    assert.equal(selected(), "T-I2");
    $("#filter-panel").classList.add("open");
    key(document.body, "j");
    key(document.body, "k");
    key(document.body, "i");
    assert.equal(selected(), "T-I2", "j and k do nothing behind the drawer");
    none($("#issue-file-dialog"));
  } finally {
    view.done();
  }
});

// ---- a refresh never moves the selection off the issue being read ----
const litRow = () => { const row = $(".issue-q-row.cursor"); return row ? row.querySelector(".issue-iid").textContent : null; };
const shownIssue = () => $("#issue-id-copy").textContent;
function link(server, shortId) {
  Object.assign(server.find(shortId), { state: "linked", tasks: [{ id: "task_9", short_id: "T-9", status: "backlog", title: "Fix it" }] });
}

test("an issue that leaves its queue while being read stays selected and shown, updated in place", async () => {
  const server = makeServer([issue(1), issue(2, { title: "Being read" }), issue(3)]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    key(document.body, "j");
    await flush();
    assert.equal(shownIssue(), "T-I2");
    const title = $("#issue-d-title");
    const body = $("#issue-d-body");
    body.scrollTop = 120;

    link(server, "T-I2");
    await view.dashboard.refresh(); await flush();
    assert.equal(shownIssue(), "T-I2", "still showing the issue being read");
    same($("#issue-d-title"), title, "the same detail, not a re-mount");
    assert.equal(title.textContent, "Being read");
    assert.equal($("#issue-d-state").textContent, "linked", "updated in place");
    assert.match($("#issue-outcome").textContent, /T-9/);
    assert.equal(body.scrollTop, 120, "detail scroll kept");
    assert.equal($$(".issue-q-row").length, 2, "it left the Open rows");
    none($(".issue-q-row.cursor"), "no row is lit: the selected issue is not in this queue");

    for (let i = 0; i < 2; i++) { await view.dashboard.refresh(); await flush(); }
    assert.equal(shownIssue(), "T-I2", "later polls do not move it either");
    none($(".issue-q-row.cursor"));
  } finally {
    view.done();
  }
});

test("the selected issue remains selected after visiting Board and returning to Issues", async () => {
  const server = makeServer([issue(1), issue(2), issue(3)]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    key(document.body, "j");
    assert.equal(shownIssue(), "T-I2");

    link(server, "T-I2");
    await view.dashboard.refresh(); await flush();
    assert.equal(shownIssue(), "T-I2");
    none(litRow(), "the selected issue left the Open queue before leaving for Board");

    view.dashboard.deactivate(); // the dashboard's Board view owns the page
    view.dashboard.render(); // returning to Issues restores the prior selection
    await flush();
    assert.equal(shownIssue(), "T-I2");
    none(litRow(), "the selected issue remains shown without lighting a row outside its queue");
  } finally {
    view.done();
  }
});

test("after a paused video's issue left its queue, the poll keeps it and the highlight follows it back", async () => {
  const server = makeServer([issue(1, { media: [VIDEO] }), issue(2), issue(3)]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    const video = $("#issue-d-media video");
    $("[data-play]").click();
    link(server, "T-I1");
    await view.dashboard.refresh(); await flush();
    video.pause();
    await view.dashboard.refresh(); await flush();
    assert.equal(shownIssue(), "T-I1", "pausing does not let a poll move the selection");
    same($("#issue-d-media video"), video, "the same video, at the same point");
    none($(".issue-q-row.cursor"), "no other row is lit");

    Object.assign(server.find("T-I1"), { state: "open", tasks: [] }); // unlinked: back in Open
    await view.dashboard.refresh(); await flush();
    assert.equal(litRow(), "T-I1", "back in the queue, its row is lit");
    assert.equal($$(".issue-q-row.cursor").length, 1);
  } finally {
    view.done();
  }
});

test("j and k from an issue that left the queue step from where it would sit", async () => {
  const server = makeServer([issue(1), issue(2), issue(3), issue(4)]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    key(document.body, "j");
    link(server, "T-I2");
    await view.dashboard.refresh(); await flush();
    assert.equal(shownIssue(), "T-I2");
    key(document.body, "j");
    assert.equal(litRow(), "T-I3", "j: the issue that came after it");
    assert.equal(shownIssue(), "T-I3");

    link(server, "T-I3");
    await view.dashboard.refresh(); await flush();
    assert.equal(shownIssue(), "T-I3");
    key(document.body, "k");
    assert.equal(litRow(), "T-I1", "k: the issue that came before it");
    key(document.body, "j");
    assert.equal(litRow(), "T-I4");
  } finally {
    view.done();
  }
});

test("when the selected issue is gone from the board, the row the cursor lands on is lit", async () => {
  const server = makeServer([issue(1), issue(2), issue(3)]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    key(document.body, "j");
    link(server, "T-I2");
    await view.dashboard.refresh(); await flush();
    none($(".issue-q-row.cursor"));
    server.details.splice(1, 1); // gone, while the Open rows stay the same
    await view.dashboard.refresh(); await flush();
    assert.equal(shownIssue(), "T-I3");
    assert.equal(litRow(), "T-I3", "the highlight moves with the cursor even when the rows did not change");
  } finally {
    view.done();
  }
});

// ---- close and reopen in the detail header ----
const triage = () => $("#issue-triage");
const popOn = () => $("#issue-close-pop").classList.contains("on");
function typeReason(text) {
  const input = $("#issue-close-reason");
  input.value = text;
  input.dispatchEvent(new Event("input", { bubbles: true }));
}
// What the dashboard server does on a dismiss or reopen, for the fake API.
function closeOnServer(server, shortId, reason) {
  Object.assign(server.find(shortId), { state: "dismissed", closure: { kind: "dismiss", reason } });
}
function reopenOnServer(server, shortId) {
  Object.assign(server.find(shortId), { state: "open", closure: null });
}

test("the header button reads Close for a live issue and Reopen for a closed one, and keeps its slot with nothing selected", async () => {
  const server = makeServer([issue(1), issue(2, { state: "dismissed", closure: { kind: "dismiss", reason: "No" } })]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    assert.equal(triage().textContent, "Close");
    assert.equal(triage().classList.contains("off"), false);
    assert.equal(triage().disabled, false);
    for (const state of ["linked", "resolved"]) {
      server.find("T-I1").state = state;
      await view.dashboard.refresh(); await flush();
      assert.equal(triage().textContent, "Close", state);
      assert.equal(triage().classList.contains("off"), false, state);
    }
    server.find("T-I1").state = "open";
    await view.dashboard.refresh(); await flush();
    key(document.body, "4");
    await flush();
    assert.equal(shownIssue(), "T-I2");
    assert.equal(triage().textContent, "Reopen");
    assert.equal(triage().classList.contains("off"), false);
    key(document.body, "3");
    await flush();
    assert.equal(shownIssue(), "", "nothing is selected in the empty Resolved queue");
    assert.ok(triage(), "the slot is still in the header");
    assert.equal(triage().classList.contains("off"), true, "hidden, not removed");
    assert.equal(triage().disabled, true);
  } finally {
    view.done();
  }
});

test("Close opens a reason box; a blank reason posts nothing; a reason posts to the dismiss route", async () => {
  const server = makeServer([issue(1), issue(2)]);
  const view = boot(server, { apiPost: (url, body) => { closeOnServer(server, "T-I1", body.reason); return Promise.resolve({}); } });
  try {
    view.dashboard.render();
    await flush();
    assert.equal(popOn(), false);
    triage().click();
    assert.equal(popOn(), true);
    same(document.activeElement, $("#issue-close-reason"), "the box takes focus");
    assert.equal($("#issue-close-go").disabled, true, "confirm waits for a reason");
    key($("#issue-close-reason"), "Enter");
    typeReason("   ");
    assert.equal($("#issue-close-go").disabled, true, "blank text is still blank");
    key($("#issue-close-reason"), "Enter");
    $("#issue-close-go").click();
    await flush();
    assert.equal(view.posts.length, 0, "nothing was posted");

    typeReason("  Works as designed ");
    assert.equal($("#issue-close-go").disabled, false);
    key($("#issue-close-reason"), "Enter");
    key($("#issue-close-reason"), "Enter"); // a double Enter must not post twice
    await flush();
    assert.deepEqual(view.posts, [{ url: "/api/issues/iss_1/dismiss", body: { reason: "Works as designed" } }]);
    assert.equal(popOn(), false, "the box closes on success");
    assert.equal($("#issue-close-reason").value, "", "and is cleared");
    assert.equal(shownIssue(), "T-I1", "the issue stays selected after it leaves its queue");
    assert.equal(triage().textContent, "Reopen");
    assert.match($("#issue-outcome").textContent, /Dismissed\. Works as designed/);
    assert.equal($$(".issue-q-row").length, 1, "it left the Open rows");
  } finally {
    view.done();
  }
});

test("Reopen posts at once, with no box", async () => {
  const server = makeServer([issue(1, { state: "dismissed", closure: { kind: "dismiss", reason: "No" } })]);
  const view = boot(server, { apiPost: () => { reopenOnServer(server, "T-I1"); return Promise.resolve({}); } });
  try {
    view.dashboard.render();
    await flush();
    key(document.body, "4");
    await flush();
    assert.equal(triage().textContent, "Reopen");
    triage().click();
    await flush();
    assert.deepEqual(view.posts, [{ url: "/api/issues/iss_1/reopen", body: {} }]);
    assert.equal(popOn(), false);
    assert.equal(triage().textContent, "Close");
    assert.equal($("#issue-d-state").textContent, "open");
  } finally {
    view.done();
  }
});

test("a refused close keeps the box and the typed reason and shows the message inline; a refused reopen toasts", async () => {
  const server = makeServer([issue(1), issue(2, { state: "dismissed", closure: { kind: "dismiss", reason: "No" } })]);
  const view = boot(server, { apiPost: () => Promise.reject(new Error("Issue is already closed.")) });
  try {
    view.dashboard.render();
    await flush();
    triage().click();
    typeReason("Because");
    $("#issue-close-go").click();
    await flush();
    assert.equal(popOn(), true, "the box stays");
    assert.equal($("#issue-close-reason").value, "Because", "the text stays");
    assert.equal($("#issue-close-err").textContent, "Issue is already closed.");
    assert.equal($("#issue-close-go").disabled, false, "it can be sent again");
    assert.deepEqual(view.toasts, [], "no toast: the message is in the box");
    typeReason("Because, still");
    assert.equal($("#issue-close-err").textContent, "", "typing clears the message");

    key(document.body, "4");
    await flush();
    triage().click();
    await flush();
    assert.deepEqual(view.toasts, ["Issue is already closed."]);
  } finally {
    view.done();
  }
});

test("a refresh leaves an open reason box, its text and its focus alone; Escape and a new selection close it", async () => {
  const server = makeServer([issue(1), issue(2)]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    triage().click();
    typeReason("half a reas");
    const input = $("#issue-close-reason");
    for (let i = 0; i < 3; i++) {
      await view.dashboard.refresh(); await flush();
      same($("#issue-close-reason"), input, "the same input");
      assert.equal(popOn(), true);
      assert.equal(input.value, "half a reas");
      same(document.activeElement, input, "focus kept");
      assert.equal($("#issue-close-go").disabled, false);
    }
    server.comment("T-I1", "agent:claude", "a note while typing");
    await view.dashboard.refresh(); await flush();
    assert.equal(input.value, "half a reas");
    same(document.activeElement, input);

    // Queue keys are inert while typing in the box.
    for (const name of ["j", "k", "c", "f", "1", "2", "3", "4"]) key(input, name);
    await flush();
    assert.equal(shownIssue(), "T-I1");
    assert.equal(popOn(), true);

    const escape = key(input, "Escape");
    assert.equal(escape.defaultPrevented, true);
    assert.equal(popOn(), false);
    assert.equal(input.value, "");
    same(document.activeElement, triage(), "focus returns to the button");

    triage().click();
    typeReason("for the first one");
    $$(".issue-q-row")[1].click();
    await flush();
    assert.equal(shownIssue(), "T-I2");
    assert.equal(popOn(), false, "a new selection closes the box");
    assert.equal($("#issue-close-reason").value, "", "and clears it");
  } finally {
    view.done();
  }
});

test("a box left open on an issue that someone else closes is dropped, as closing no longer applies", async () => {
  const server = makeServer([issue(1)]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    triage().click();
    typeReason("mine");
    closeOnServer(server, "T-I1", "theirs");
    await view.dashboard.refresh(); await flush();
    assert.equal(popOn(), false);
    assert.equal(triage().textContent, "Reopen");
  } finally {
    view.done();
  }
});

test("keys typed on the reason box's buttons or Space on the header button stay out of the Inbox's own keys", async () => {
  const server = makeServer([issue(1, { media: [VIDEO] }), issue(2)]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    triage().click();
    typeReason("keep me");
    for (const target of [$("#issue-close-cancel"), $("#issue-close-go")]) {
      target.focus();
      for (const name of ["j", "k", "ArrowDown", "1", "2", "3", "4", "c", "f", " "]) {
        const event = key(target, name);
        assert.equal(event.defaultPrevented, false, name + " is left to the button");
      }
      assert.equal(shownIssue(), "T-I1", "selection did not move");
      assert.equal(popOn(), true, "the box stayed open");
      assert.equal($("#issue-close-reason").value, "keep me", "the reason stayed");
    }
    key($("#issue-close-reason"), "Escape");
    triage().focus();
    assert.equal(key(triage(), " ").defaultPrevented, false, "Space on the header button is a press, not play or pause");
    assert.equal(shownIssue(), "T-I1");
  } finally {
    view.done();
  }
});

test("Escape closes the reason box from Cancel and from Close issue, and returns focus to the header button", async () => {
  const server = makeServer([issue(1)]);
  const view = boot(server);
  try {
    view.dashboard.render();
    await flush();
    for (const id of ["issue-close-cancel", "issue-close-go", "issue-close-reason"]) {
      triage().click();
      typeReason("something");
      $("#" + id).focus();
      const event = key($("#" + id), "Escape");
      assert.equal(event.defaultPrevented, true, id);
      assert.equal(popOn(), false, id + " closed the box");
      assert.equal($("#issue-close-reason").value, "", id + " cleared it");
      same(document.activeElement, triage(), id + " returned focus to the button");
    }
  } finally {
    view.done();
  }
});

test("after a successful close, focus lands on the header button, which now reads Reopen", async () => {
  const server = makeServer([issue(1)]);
  const view = boot(server, { apiPost: (url, body) => { closeOnServer(server, "T-I1", body.reason); return Promise.resolve({}); } });
  try {
    view.dashboard.render();
    await flush();
    triage().click();
    typeReason("done with it");
    $("#issue-close-go").focus();
    $("#issue-close-go").click();
    await flush();
    assert.equal(popOn(), false);
    assert.equal(triage().textContent, "Reopen");
    same(document.activeElement, triage(), "focus is on the header button, not the page body");
  } finally {
    view.done();
  }
});

test("a double click on Reopen posts once", async () => {
  const server = makeServer([issue(1, { state: "dismissed", closure: { kind: "dismiss", reason: "No" } })]);
  let release;
  const view = boot(server, { apiPost: () => new Promise((resolve) => { release = () => { reopenOnServer(server, "T-I1"); resolve({}); }; }) });
  try {
    view.dashboard.render();
    await flush();
    key(document.body, "4");
    await flush();
    triage().click();
    triage().click();
    await flush();
    assert.equal(view.posts.length, 1, "the second click was ignored while the first was in flight");
    release();
    await flush();
    assert.equal(triage().textContent, "Close");
  } finally {
    view.done();
  }
});
