"use strict";

// Tests for the hosted dashboard's live refresh and retry-safe writes
// (live.js; SPEC §8.6, §10, AC-24). node's built-in runner, zero deps; bridged
// into pytest by tests/test_dashboard/test_js_live.py. Standalone:
//   node --test tests/js/live.test.js

const test = require("node:test");
const assert = require("node:assert/strict");
const crypto = require("node:crypto");

const {
  newOpId,
  hostedSlug,
  streamUrl,
  retryDelay,
  createLiveRefresh,
  createRefreshScheduler,
  createWriter,
  WRITE_ATTEMPTS,
} = require("../../src/lattice/dashboard/static/live.js");

const OP_ID_RE = /^op_[0-9A-HJKMNP-TV-Z]{26}$/; // the server's check (ops/base.py)

test("newOpId is a ULID op id the server accepts, sortable by time", () => {
  const random = (n) => crypto.randomBytes(n);
  const a = newOpId(1790000000000, random);
  const b = newOpId(1790000000001, random);
  assert.match(a, OP_ID_RE);
  assert.match(b, OP_ID_RE);
  assert.ok(a.slice(3, 13) < b.slice(3, 13));
  const ids = new Set(Array.from({ length: 500 }, () => newOpId(Date.now(), random)));
  assert.equal(ids.size, 500);
});

test("newOpId encodes time and randomness deterministically", () => {
  const zeros = () => new Uint8Array(16);
  assert.equal(newOpId(0, zeros), "op_" + "0".repeat(26));
  const all31 = () => new Uint8Array(16).fill(31);
  assert.equal(newOpId(0, all31), "op_" + "0".repeat(10) + "Z".repeat(16));
});

test("hostedSlug reads /p/<slug>/ and nothing else", () => {
  assert.equal(hostedSlug("/p/alpha/"), "alpha");
  assert.equal(hostedSlug("/p/my-proj-2/index.html"), "my-proj-2");
  assert.equal(hostedSlug("/"), null);
  assert.equal(hostedSlug("/p/alpha"), null);
  assert.equal(hostedSlug("/p/Alpha/"), null);
  assert.equal(hostedSlug("/p/-x/"), null);
  assert.equal(hostedSlug("/x/p/alpha/"), null);
  assert.equal(hostedSlug(undefined), null);
});

test("streamUrl is the project's /v1 stream", () => {
  assert.equal(streamUrl("alpha"), "/v1/projects/alpha/stream");
});

test("retryDelay retries only what SPEC §8.6's client retries", () => {
  for (const status of [0, 429, 502, 504]) {
    assert.equal(retryDelay(1, status, null, null), 500, `status ${status}`);
  }
  assert.equal(retryDelay(1, 503, "BOARD_BUSY", null), 500);
  assert.equal(retryDelay(1, 503, "BOARD_UNAVAILABLE", null), null);
  for (const status of [200, 201, 400, 401, 403, 404, 409, 413, 422, 500]) {
    assert.equal(retryDelay(1, status, "X", null), null, `status ${status}`);
  }
});

test("retryDelay backs off, honors Retry-After, and stops", () => {
  assert.deepEqual([1, 2, 3].map((a) => retryDelay(a, 0, null, null)), [500, 1000, 2000]);
  assert.equal(retryDelay(1, 429, "RATE_LIMITED", "2"), 2000);
  assert.equal(retryDelay(1, 429, "RATE_LIMITED", "600"), 30000);
  assert.equal(retryDelay(WRITE_ATTEMPTS, 0, null, null), null);
});

// A fake EventSource the tests drive by hand.
class FakeSource {
  constructor(url) {
    this.url = url;
    this.listeners = {};
    this.closed = false;
    FakeSource.last = this;
  }
  addEventListener(type, fn) {
    (this.listeners[type] = this.listeners[type] || []).push(fn);
  }
  emit(type) {
    for (const fn of this.listeners[type] || []) fn({ type });
  }
  close() {
    this.closed = true;
  }
}

function rig() {
  const log = [];
  let release = null;
  const scheduler = createRefreshScheduler(() => {
    log.push("refresh");
    return new Promise((resolve) => { release = resolve; });
  });
  const live = createLiveRefresh({
    url: "/v1/projects/alpha/stream",
    EventSource: FakeSource,
    schedule: scheduler.trigger,
    startPoll: () => log.push("poll:on"),
    stopPoll: () => log.push("poll:off"),
  });
  return { live, log, scheduler, finish: () => release && release() };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

test("the poll runs until the stream opens; opening catches up once", async () => {
  const { live, log, finish } = rig();
  live.connect();
  assert.equal(FakeSource.last.url, "/v1/projects/alpha/stream");
  assert.deepEqual(log, ["poll:off", "poll:on"]);
  FakeSource.last.emit("open");
  assert.equal(live.isOpen(), true);
  assert.deepEqual(log.slice(2), ["poll:off", "refresh"]);
  finish();
  await tick();
});

test("every entry refetches; entries during a refetch queue exactly one more", async () => {
  const { live, log, finish } = rig();
  live.connect();
  FakeSource.last.emit("open");
  finish();
  await tick();
  log.length = 0;
  FakeSource.last.emit("journal");
  assert.deepEqual(log, ["refresh"]);
  FakeSource.last.emit("journal");
  FakeSource.last.emit("journal");
  assert.deepEqual(log, ["refresh"]); // one in flight, one queued
  finish();
  await tick();
  assert.deepEqual(log, ["refresh", "refresh"]);
  finish();
  await tick();
  assert.deepEqual(log, ["refresh", "refresh"]);
  FakeSource.last.emit("reset");
  assert.deepEqual(log, ["refresh", "refresh", "refresh"]);
  finish();
  await tick();
});

test("a stream error falls back to the poll; reopening stops it", async () => {
  const { live, log, finish } = rig();
  live.connect();
  FakeSource.last.emit("open");
  finish();
  await tick();
  log.length = 0;
  FakeSource.last.emit("error");
  assert.equal(live.isOpen(), false);
  assert.deepEqual(log, ["poll:on"]);
  FakeSource.last.emit("open");
  assert.deepEqual(log, ["poll:on", "poll:off", "refresh"]);
  finish();
  await tick();
});

test("close ends the stream and the poll; a failing refresh never wedges it", async () => {
  const log = [];
  const scheduler = createRefreshScheduler(() => { log.push("refresh"); throw new Error("offline"); });
  const live = createLiveRefresh({
    url: "/s",
    EventSource: FakeSource,
    schedule: scheduler.trigger,
    startPoll: () => {},
    stopPoll: () => log.push("poll:off"),
  });
  live.connect();
  const source = FakeSource.last;
  source.emit("journal");
  await tick();
  source.emit("journal");
  await tick();
  assert.deepEqual(log.filter((x) => x === "refresh"), ["refresh", "refresh"]);
  live.close();
  assert.equal(source.closed, true);
  assert.equal(live.isOpen(), false);
});

// --- createHeadWatch: the local dashboard on a bound checkout ---

const { createHeadWatch } = require("../../src/lattice/dashboard/static/live.js");

function headRig(heads, schedule) {
  const log = [];
  let answer = 0;
  const timers = [];
  const watch = createHeadWatch({
    fetchHead: () => {
      const next = heads[Math.min(answer++, heads.length - 1)];
      if (next instanceof Error) return Promise.reject(next);
      return Promise.resolve(next);
    },
    schedule: schedule || (() => { log.push("refresh"); }),
    intervalMs: 1000,
    setInterval: (fn) => { timers.push(fn); return timers.length; },
    clearInterval: () => log.push("stopped"),
  });
  return { watch, log };
}

test("a moved head refetches once; an unchanged head does nothing", async () => {
  const h = (seq) => ({ epoch: "ep_1", seq });
  const { watch, log } = headRig([h(3), h(3), h(4), h(4), { epoch: "ep_2", seq: 1 }]);
  watch.start(); // first answer: refetches, since the page's first read came before it
  await tick();
  assert.deepEqual(log, ["refresh"]);
  for (let i = 0; i < 4; i++) { await watch.check(); await tick(); }
  assert.deepEqual(log, ["refresh", "refresh", "refresh"]); // seq 3 -> 4, then the new epoch
  assert.equal(watch.isRunning(), true);
});

test("a plain local board (head null) stops the watch; the poll carries on", async () => {
  const { watch, log } = headRig([null]);
  watch.start();
  await tick();
  assert.equal(watch.isRunning(), false);
  assert.deepEqual(log, ["stopped"]);
});

test("a failed head request never refetches or stops the watch", async () => {
  const { watch, log } = headRig([{ epoch: "e", seq: 1 }, new Error("down"), { epoch: "e", seq: 1 }]);
  watch.start();
  await tick();
  log.length = 0; // the first answer's refetch
  await watch.check();
  await watch.check();
  assert.deepEqual(log, []);
  assert.equal(watch.isRunning(), true);
});

test("a write between the page's first read and the watch's start is never missed", async () => {
  // The board is at seq 3 when the page first reads it; a write lands (seq 4)
  // before the watch's first head read. That first read must refetch.
  let seq = 3;
  const rendered = [];
  const scheduler = createRefreshScheduler(() => { rendered.push(seq); });
  rendered.push(seq); // the page's first read
  seq = 4; // the write in between
  const watch = createHeadWatch({
    fetchHead: () => Promise.resolve({ epoch: "e", seq }),
    schedule: scheduler.trigger,
    intervalMs: 1000,
    setInterval: () => 1,
    clearInterval: () => {},
  });
  watch.start();
  await tick();
  assert.equal(rendered[rendered.length - 1], 4);
});

// --- One refresh scheduler for the stream, the head watch, and the poll ---

test("stream, head watch, and poll share one refresh: never two at once", async () => {
  let running = 0;
  let most = 0;
  const releases = [];
  const scheduler = createRefreshScheduler(() => {
    running++;
    most = Math.max(most, running);
    return new Promise((resolve) => releases.push(() => { running--; resolve(); }));
  });
  const live = createLiveRefresh({
    url: "/s",
    EventSource: FakeSource,
    schedule: scheduler.trigger,
    startPoll: () => {},
    stopPoll: () => {},
  });
  live.connect();
  const watch = createHeadWatch({
    fetchHead: () => Promise.resolve({ epoch: "e", seq: releases.length }),
    schedule: scheduler.trigger,
    intervalMs: 1000,
    setInterval: () => 1,
    clearInterval: () => {},
  });
  FakeSource.last.emit("open"); // refresh 1 starts
  watch.start(); // head seen: queued
  await tick();
  scheduler.trigger(); // the poll: queued (already pending)
  FakeSource.last.emit("journal"); // queued (already pending)
  assert.equal(releases.length, 1);
  releases[0]();
  await tick();
  assert.equal(releases.length, 2); // exactly one more, after the first ended
  releases[1]();
  await tick();
  assert.equal(releases.length, 2);
  assert.equal(most, 1);
});

// --- createWriter: the page's writes ---

function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

function answer(status, body, headers = {}) {
  return {
    status,
    headers: { get: (name) => headers[name] ?? null },
    json: () => (body instanceof Error ? Promise.reject(body) : Promise.resolve(body)),
  };
}

function writerRig({ hosted = true, responses }) {
  const calls = [];
  const sleeps = [];
  let n = 0;
  const writer = createWriter({
    fetch: (url, opts) => {
      calls.push({ url, opId: opts.headers["Lattice-Op-Id"], body: opts.body });
      const next = responses[Math.min(n++, responses.length - 1)];
      return typeof next === "function" ? next() : next;
    },
    url: (path) => "/p/alpha" + path,
    hosted,
    newOpId: () => newOpId(Date.now(), (k) => crypto.randomBytes(k)),
    sleep: (ms) => { sleeps.push(ms); return Promise.resolve(); },
  });
  return { writer, calls, sleeps };
}

test("a double click applies once; the next action gets a fresh op_id", async () => {
  const first = deferred();
  const { writer, calls } = writerRig({
    responses: [() => first.promise, () => Promise.resolve(answer(201, { ok: true, data: { id: "t2" } }))],
  });
  const a = writer.post("/api/tasks", { title: "x" });
  const b = writer.post("/api/tasks", { title: "x" }); // the second click, synchronously
  assert.equal(a, b);
  assert.equal(writer.pending(), 1);
  first.resolve(answer(201, { ok: true, data: { id: "t1" } }));
  assert.deepEqual(await a, { id: "t1" });
  assert.deepEqual(await b, { id: "t1" });
  await tick();
  assert.equal(writer.pending(), 0);
  await tick();
  assert.equal(calls.length, 1);
  assert.deepEqual(await writer.post("/api/tasks", { title: "x" }), { id: "t2" });
  assert.equal(calls.length, 2);
  assert.match(calls[1].opId, OP_ID_RE);
  assert.notEqual(calls[1].opId, calls[0].opId);
});

test("different writes in flight are sent separately; a failure clears the guard", async () => {
  const { writer, calls } = writerRig({
    responses: [answer(422, { ok: false, error: { code: "PLAN_REQUIRED", message: "no plan" } })],
  });
  const a = writer.post("/api/tasks/t/status", { status: "in_progress" });
  const b = writer.post("/api/tasks/t/comment", { body: "hi" });
  await assert.rejects(a, /no plan/);
  await assert.rejects(b, /no plan/);
  assert.equal(calls.length, 2);
  await tick();
  assert.equal(writer.pending(), 0);
});

test("a success whose body cannot be read is retried with the same op_id", async () => {
  const { writer, calls, sleeps } = writerRig({
    responses: [
      answer(201, new SyntaxError("Unexpected end of JSON input")), // committed, body cut off
      answer(201, { ok: true, data: { id: "t1" } }), // the replay's stored answer
    ],
  });
  assert.deepEqual(await writer.post("/api/tasks", { title: "x" }), { id: "t1" });
  assert.equal(calls.length, 2);
  assert.match(calls[0].opId, OP_ID_RE);
  assert.equal(calls[1].opId, calls[0].opId);
  assert.equal(calls[1].body, calls[0].body);
  assert.deepEqual(sleeps, [500]);
});

test("a lost response and a transient refusal retry with the same op_id; a refusal does not", async () => {
  const { writer, calls, sleeps } = writerRig({
    responses: [
      () => Promise.reject(new TypeError("Failed to fetch")),
      answer(503, { ok: false, error: { code: "BOARD_BUSY", message: "busy" } }, { "Retry-After": "1" }),
      answer(200, { ok: true, data: { status: "planned" } }),
    ],
  });
  assert.deepEqual(await writer.post("/api/tasks/t/status", { status: "planned" }), { status: "planned" });
  assert.equal(new Set(calls.map((c) => c.opId)).size, 1);
  assert.deepEqual(sleeps, [500, 1000]);
  const refused = writerRig({
    responses: [answer(409, { ok: false, error: { code: "CONFLICT", message: "stale" } })],
  });
  await assert.rejects(refused.writer.post("/api/x", {}), /stale/);
  assert.equal(refused.calls.length, 1);
});

test("an unknown outcome that never resolves stops after WRITE_ATTEMPTS and says so", async () => {
  const { writer, calls } = writerRig({ responses: [answer(201, new SyntaxError("cut"))] });
  await assert.rejects(writer.post("/api/tasks", { title: "x" }), /may have applied/);
  assert.equal(calls.length, WRITE_ATTEMPTS);
  assert.equal(new Set(calls.map((c) => c.opId)).size, 1);
});

test("locally a write is sent once, without an op_id, and never retried", async () => {
  const { writer, calls } = writerRig({ hosted: false, responses: [answer(201, new SyntaxError("cut"))] });
  await assert.rejects(writer.post("/api/tasks", { title: "x" }), /may have applied/);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].opId, undefined);
  const lost = writerRig({ hosted: false, responses: [() => Promise.reject(new TypeError("down"))] });
  await assert.rejects(lost.writer.post("/api/tasks", {}), /did not answer/);
  assert.equal(lost.calls.length, 1);
});
