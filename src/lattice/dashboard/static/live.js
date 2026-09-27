"use strict";

// --- Live refresh and retry-safe writes (SPEC §8.6, §10) ---
// On a hosted dashboard (/p/<slug>/), the page follows the project's change
// stream (/v1/projects/<slug>/stream, authenticated by the session cookie) and
// refetches its current view on every entry, falling back to the 5-second poll
// whenever the stream is down. Every write names one op_id per logical action,
// reused on each retry (Lattice-Op-Id), so a retry after a lost or unreadable
// response applies once. Locally (served at "/"), the poll runs, a bound
// checkout's head is watched, and writes are never retried (a local board has no
// idempotency index). Everywhere, one refresh runs at a time (one scheduler for
// the stream, the head watch, and the poll), and a write identical to one still
// in flight joins it rather than sending again (a double click applies once).
//
// Like escape.js: a classic script loaded WITHOUT defer before the inline IIFE,
// ES5-flavored, with a CommonJS export guard for node:test (tests/js/live.test.js).

var OP_ID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"; // Crockford base32, as ULIDs

// "op_" + a ULID: 10 characters of millisecond time, then 16 random ones.
// randomBytes(n) returns n random bytes (crypto.getRandomValues in the page).
function newOpId(nowMs, randomBytes) {
  var t = Math.max(0, Math.floor(nowMs));
  var time = "";
  for (var i = 0; i < 10; i++) {
    time = OP_ID_ALPHABET.charAt(t % 32) + time;
    t = Math.floor(t / 32);
  }
  var bytes = randomBytes(16);
  var rand = "";
  for (var j = 0; j < 16; j++) rand += OP_ID_ALPHABET.charAt(bytes[j] % 32);
  return "op_" + time + rand;
}

// The project slug of a hosted dashboard page ("/p/<slug>/..."), else null.
function hostedSlug(pathname) {
  var m = /^\/p\/([a-z0-9][a-z0-9-]{0,62})\//.exec(String(pathname || ""));
  return m ? m[1] : null;
}

// The change stream a hosted page follows.
function streamUrl(slug) {
  return "/v1/projects/" + slug + "/stream";
}

var WRITE_ATTEMPTS = 4;

// How long to wait before retrying a hosted write, in ms, or null to stop.
// status 0 means the request failed without a response. Retried: no response,
// 429, 502, 504, and 503 unless the project is unavailable (SPEC §8.6's client
// rule); a Retry-After header, when given, sets the wait.
function retryDelay(attempt, status, errorCode, retryAfter) {
  if (attempt >= WRITE_ATTEMPTS) return null;
  var retryable = status === 0 || status === 429 || status === 502 || status === 504
    || (status === 503 && errorCode !== "BOARD_UNAVAILABLE");
  if (!retryable) return null;
  var seconds = parseInt(retryAfter, 10);
  if (!isNaN(seconds) && seconds >= 0) return Math.min(seconds, 30) * 1000;
  return Math.min(5000, 500 * Math.pow(2, attempt - 1));
}

// The page's one refresh path: every source (the stream, the head watch, the
// poll) calls trigger(). refresh() refetches the current view (it may return a
// promise); a trigger while one runs queues exactly one more, so fetches never
// overlap (no stale answer rendered over a newer one) and the view always ends
// at the newest change.
function createRefreshScheduler(refresh) {
  var running = false;
  var pending = false;

  function trigger() {
    if (running) { pending = true; return; }
    running = true;
    var done = function() {
      running = false;
      if (pending) { pending = false; trigger(); }
    };
    var result;
    try { result = refresh(); } catch (e) { result = null; }
    Promise.resolve(result).then(done, done);
  }

  return { trigger: trigger, isRunning: function() { return running; } };
}

// The page's writes. opts: {fetch, url(path), hosted, newOpId(), sleep(ms)}.
// post(path, data) resolves to the answer's data or rejects with its message.
//
// - One logical write, one op_id: hosted, every attempt carries the same
//   Lattice-Op-Id, retried (retryDelay) on no response, a transient refusal, or
//   an answer whose body cannot be read, since then the write's outcome is
//   unknown; the server applies the op_id once (SPEC §8.6).
// - A post identical (path and body) to one still in flight returns that
//   post's promise instead of sending: a double click applies once, and the
//   next action, once it settles, gets a fresh op_id.
function createWriter(opts) {
  var inflight = {};

  function send(path, body) {
    var opId = opts.hosted ? opts.newOpId() : null;
    function attempt(n) {
      var headers = {"Content-Type": "application/json"};
      if (opId) headers["Lattice-Op-Id"] = opId;
      return Promise.resolve()
        .then(function() {
          return opts.fetch(opts.url(path), {method: "POST", headers: headers, body: body});
        })
        .then(function(r) {
          return Promise.resolve()
            .then(function() { return r.json(); })
            .then(function(b) { return {r: r, body: b || null}; },
                  function() { return {r: r, body: null}; });
        }, function() { return {r: null, body: null}; })
        .then(function(res) {
          var r = res.r;
          var b = res.body;
          if (opId) {
            // No readable answer (no response, or a body that would not parse)
            // is an unknown outcome: retried like a lost response.
            var status = b ? r.status : 0;
            var code = b && b.error ? b.error.code : null;
            var retryAfter = r && r.headers ? r.headers.get("Retry-After") : null;
            var wait = retryDelay(n, status, code, retryAfter);
            if (wait !== null) return opts.sleep(wait).then(function() { return attempt(n + 1); });
          }
          if (!r) throw new Error("Network error: the server did not answer");
          if (!b) throw new Error("The server's answer could not be read; the write may have applied");
          if (!b.ok) throw new Error(b.error ? b.error.message : "API error");
          return b.data;
        });
    }
    return attempt(1);
  }

  function post(path, data) {
    var body = JSON.stringify(data === undefined ? {} : data);
    var key = path + "\n" + body;
    if (Object.prototype.hasOwnProperty.call(inflight, key)) return inflight[key];
    var promise = send(path, body);
    inflight[key] = promise;
    var clear = function() { if (inflight[key] === promise) delete inflight[key]; };
    promise.then(clear, clear);
    return promise;
  }

  return { post: post, pending: function() { return Object.keys(inflight).length; } };
}

// Follow a change stream. opts: {url, EventSource, schedule, startPoll, stopPoll}.
// schedule() is the page's refresh scheduler's trigger. The poll runs only
// while the stream is not open.
function createLiveRefresh(opts) {
  var source = null;
  var open = false;
  var trigger = opts.schedule;

  function connect() {
    close();
    opts.startPoll(); // until the stream opens
    source = new opts.EventSource(opts.url);
    source.addEventListener("open", function() {
      open = true;
      opts.stopPoll();
      trigger(); // catch up on whatever changed while the stream was down
    });
    source.addEventListener("journal", trigger);
    source.addEventListener("reset", trigger);
    source.addEventListener("error", function() {
      // EventSource reconnects by itself (and stops for good on a refused
      // session); the poll covers the gap either way.
      open = false;
      opts.startPoll();
    });
  }

  function close() {
    if (source) { source.close(); source = null; }
    open = false;
    opts.stopPoll();
  }

  return {
    connect: connect,
    close: close,
    trigger: trigger,
    isOpen: function() { return open; },
  };
}

// Watch a local dashboard's head (GET /api/head, answered by `lattice dashboard`):
// on a bound checkout it is the cache's {epoch, seq}, moved by the embedded
// follower, so the page refetches within a second of any write anywhere. A
// local board answers {head: null}: the watch stops and the 5-second poll
// carries on alone. opts: {fetchHead,
// schedule, intervalMs, setInterval, clearInterval}; fetchHead() resolves to
// the head object, null, or throws; schedule() is the refresh scheduler's trigger.
//
// The first head seen also refreshes: the page's first read happened before it,
// so a write in between would otherwise stay unseen until the poll. From then
// on, every refresh follows a head read, and any later write moves the head.
function createHeadWatch(opts) {
  var timer = null;
  var last;          // undefined until the first answer

  function check() {
    var answer;
    try { answer = opts.fetchHead(); } catch (e) { return Promise.resolve(); }
    return Promise.resolve(answer).then(function(head) {
      if (head === null) { stop(); return; } // not a bound checkout
      var key = head.epoch + ":" + head.seq;
      if (key !== last) opts.schedule();
      last = key;
    }, function() { /* the dashboard may be restarting; the poll covers it */ });
  }

  function start() {
    if (timer !== null) return;
    timer = opts.setInterval(check, opts.intervalMs);
    check();
  }

  function stop() {
    if (timer !== null) { opts.clearInterval(timer); timer = null; }
  }

  return { start: start, stop: stop, check: check, isRunning: function() { return timer !== null; } };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    newOpId: newOpId,
    hostedSlug: hostedSlug,
    streamUrl: streamUrl,
    retryDelay: retryDelay,
    createRefreshScheduler: createRefreshScheduler,
    createWriter: createWriter,
    createLiveRefresh: createLiveRefresh,
    createHeadWatch: createHeadWatch,
    WRITE_ATTEMPTS: WRITE_ATTEMPTS,
  };
}
