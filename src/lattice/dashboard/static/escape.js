"use strict";

// --- Escaping and URLs for the dashboard page (SPEC §10) ---
// Once a board is shared, the page renders other people's strings (titles,
// comments, actors, origin lines). `esc` makes any value safe inside HTML text
// and inside a quoted attribute, single or double. The page never builds an
// event handler out of a string: elements carry `data-*` attributes and the
// page attaches listeners that read them.
//
// The same page is served at `/` by `lattice dashboard` and at `/p/<slug>/`
// by a Lattice server, so every API call and asset reference resolves against
// the page's own base path instead of the site root.
//
// Like panel-logic.js, this is a classic browser script loaded WITHOUT defer
// before the inline IIFE (names become globals), with a CommonJS export guard
// so node:test can require it (tests/js/escape.test.js, bridged into pytest).
// Keep it ES5-flavored (var, function declarations) to match.

// `s` as HTML-safe text: & < > " ' become entities; null and undefined become "".
function esc(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// The directory part of a page path: "/" -> "/", "/p/proj/" -> "/p/proj/",
// "/p/proj/index.html" -> "/p/proj/". Always starts and ends with "/".
function basePath(pathname) {
  var p = typeof pathname === "string" && pathname.charAt(0) === "/" ? pathname : "/";
  return p.slice(0, p.lastIndexOf("/") + 1);
}

// An API path ("/api/tasks" or "api/tasks") resolved under `base`.
function apiUrl(base, path) {
  var b = basePath(base);
  var rel = String(path).replace(/^\/+/, "");
  return b + rel;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { esc: esc, basePath: basePath, apiUrl: apiUrl };
}
