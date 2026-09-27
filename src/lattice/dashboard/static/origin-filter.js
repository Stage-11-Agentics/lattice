// Origin filters for the board: machine, user and worktree (AC-39).
//
// The server does the matching (GET /api/tasks?machine=&user=&worktree=, the
// same rule as `lattice list --machine/--user/--worktree`). This file only
// reads the filters from the page URL and builds that query. It is loaded as
// a plain <script> (browser globals) and is also require()-able by node for
// tests (tests/js/origin-filter.test.js).

var ORIGIN_FILTER_KEYS = ["machine", "user", "worktree"];

// An absolute POSIX worktree path normalized lexically as the CLI's
// --worktree is (and the API's): repeated and trailing slashes dropped, "."
// and ".." folded. Anything else is returned as is, and the API refuses it:
// the page has no current directory to resolve a relative path against.
function normalizeWorktree(path) {
  var raw = String(path);
  if (raw.charAt(0) !== "/") return raw;
  var lead = "/";
  var out = [];
  raw.split("/").forEach(function(part) {
    if (part === "" || part === ".") return;
    if (part === "..") { if (out.length) out.pop(); return; }
    out.push(part);
  });
  return lead + out.join("/");
}

// {machine, user, worktree} from a location.search string; an absent or empty
// parameter is null (no filter).
function originFiltersFromSearch(search) {
  var sp = new URLSearchParams(search || "");
  var filters = {};
  ORIGIN_FILTER_KEYS.forEach(function(key) {
    var value = sp.get(key);
    filters[key] = value ? value : null;
  });
  if (filters.worktree !== null) filters.worktree = normalizeWorktree(filters.worktree);
  return filters;
}

// How many origin filters are set.
function originFilterCount(filters) {
  var n = 0;
  ORIGIN_FILTER_KEYS.forEach(function(key) { if (filters && filters[key]) n++; });
  return n;
}

// Whether a tag, assignee or creator selection absent from the loaded tasks
// stays selected. Unfiltered, the loaded tasks are the whole board, so an
// absent value is stale and clears. Under an origin filter they are a subset:
// absence proves nothing, and the selection stays so the filters AND (to an
// empty board when they are disjoint).
function keepsAbsentSelection(originFilters) {
  return originFilterCount(originFilters) > 0;
}

// The API path to fetch: "/api/tasks" gains the set origin filters as its
// query; every other path, or no filter set, is returned unchanged.
function withOriginFilters(path, filters) {
  if (path !== "/api/tasks" || originFilterCount(filters) === 0) return path;
  var sp = new URLSearchParams();
  ORIGIN_FILTER_KEYS.forEach(function(key) { if (filters[key]) sp.set(key, filters[key]); });
  return path + "?" + sp.toString();
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    ORIGIN_FILTER_KEYS: ORIGIN_FILTER_KEYS,
    normalizeWorktree: normalizeWorktree,
    originFiltersFromSearch: originFiltersFromSearch,
    originFilterCount: originFilterCount,
    keepsAbsentSelection: keepsAbsentSelection,
    withOriginFilters: withOriginFilters,
  };
}
