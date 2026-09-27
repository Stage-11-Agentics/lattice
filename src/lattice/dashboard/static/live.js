"use strict";

// --- Live refresh and retry-safe writes (SPEC §8.6, §10) ---
// On a hosted dashboard (/p/<slug>/), the page follows the project's change
// stream (/v1/projects/<slug>/stream, authenticated by the session cookie) and
// refetches its current view on every entry, falling back to the 5-second poll
// whenever the stream is down. Every write names one op_id per logical action,
// reused on each retry (Lattice-Op-Id), so a retry after a lost response applies
// once. Locally (served at "/"), nothing here changes: the poll runs, and writes
// are never retried (a local board has no idempotency index).
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

// Follow a change stream. opts: {url, EventSource, refresh, startPoll, stopPoll}.
// refresh() refetches the current view (it may return a promise); a stream
// entry that arrives while one runs queues exactly one more, so the view always
// ends at the newest entry without overlapping fetches. The poll runs only
// while the stream is not open.
function createLiveRefresh(opts) {
  var source = null;
  var running = false;
  var pending = false;
  var open = false;

  function trigger() {
    if (running) { pending = true; return; }
    running = true;
    var done = function() {
      running = false;
      if (pending) { pending = false; trigger(); }
    };
    var result;
    try { result = opts.refresh(); } catch (e) { result = null; }
    Promise.resolve(result).then(done, done);
  }

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

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    newOpId: newOpId,
    hostedSlug: hostedSlug,
    streamUrl: streamUrl,
    retryDelay: retryDelay,
    createLiveRefresh: createLiveRefresh,
    WRITE_ATTEMPTS: WRITE_ATTEMPTS,
  };
}
