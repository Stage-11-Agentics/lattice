"use strict";

// --- Panel dismissal logic (click-outside-to-close) ---
// Pure helper for the dashboard's slide-in panels (detail panel, settings
// panel), which close on any document click that lands outside them.
//
// "Outside" must be decided from the event's propagation path, not from
// `panel.contains(event.target)`: several panel controls replace their own
// element synchronously in their click listener (inline edits swap a span for
// an input row; Cancel swaps it back). By the time the click bubbles to
// `document`, `event.target` is detached, `contains()` says false, and the
// panel closes under the user (GitHub #48). `event.composedPath()` is
// captured at dispatch, so the panel is still on it even after the target is
// removed mid-propagation.
//
// Like actor-logic.js, this is a classic browser script loaded WITHOUT defer
// before the inline IIFE (names become globals), with a CommonJS export guard
// so node:test can require it (tests/js/panel-logic.test.js, bridged into
// pytest). Keep it ES5-flavored (var, function expressions) to match.

// True iff any node on `path` (an array, e.g. from event.composedPath())
// matches one of `selectors`. Non-element entries (document, window, text
// nodes) have no `matches` and are skipped; junk in `path` never throws.
function clickPathMatches(path, selectors) {
  if (!path || !path.length || !selectors || !selectors.length) return false;
  for (var i = 0; i < path.length; i++) {
    var node = path[i];
    if (!node || typeof node.matches !== "function") continue;
    for (var j = 0; j < selectors.length; j++) {
      var hit = false;
      try { hit = node.matches(selectors[j]); } catch (e) { hit = false; }
      if (hit) return true;
    }
  }
  return false;
}

// Propagation path for a click, robust to targets removed mid-propagation.
// Falls back to walking parentNode from the target when composedPath is
// unavailable (a detached target then yields only itself and its detached
// ancestors, which is the pre-#48 behaviour — no worse than before).
function clickPath(event) {
  if (!event) return [];
  if (typeof event.composedPath === "function") {
    var p = event.composedPath();
    if (p && p.length) return p;
  }
  var out = [];
  var n = event.target;
  while (n) { out.push(n); n = n.parentNode; }
  return out;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { clickPathMatches, clickPath };
}
