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
  const live = createLiveRefresh({
    url: "/v1/projects/alpha/stream",
    EventSource: FakeSource,
    refresh: () => {
      log.push("refresh");
      return new Promise((resolve) => { release = resolve; });
    },
    startPoll: () => log.push("poll:on"),
    stopPoll: () => log.push("poll:off"),
  });
  return { live, log, finish: () => release && release() };
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
  const live = createLiveRefresh({
    url: "/s",
    EventSource: FakeSource,
    refresh: () => { log.push("refresh"); throw new Error("offline"); },
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

function headRig(heads) {
  const log = [];
  let answer = 0;
  const timers = [];
  const watch = createHeadWatch({
    fetchHead: () => {
      const next = heads[Math.min(answer++, heads.length - 1)];
      if (next instanceof Error) return Promise.reject(next);
      return Promise.resolve(next);
    },
    refresh: () => { log.push("refresh"); },
    intervalMs: 1000,
    setInterval: (fn) => { timers.push(fn); return timers.length; },
    clearInterval: () => log.push("stopped"),
  });
  return { watch, log };
}

test("a moved head refetches once; an unchanged head does nothing", async () => {
  const h = (seq) => ({ epoch: "ep_1", seq });
  const { watch, log } = headRig([h(3), h(3), h(4), h(4), { epoch: "ep_2", seq: 1 }]);
  watch.start(); // first answer: the baseline, no refetch
  await tick();
  for (let i = 0; i < 4; i++) { await watch.check(); await tick(); }
  assert.deepEqual(log, ["refresh", "refresh"]); // seq 3 -> 4, then the new epoch
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
  await watch.check();
  await watch.check();
  assert.deepEqual(log, []);
  assert.equal(watch.isRunning(), true);
});
