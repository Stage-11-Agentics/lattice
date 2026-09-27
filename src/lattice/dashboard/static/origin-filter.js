// Origin filters for the board: machine, user and worktree (AC-39).
//
// The server does the matching (GET /api/tasks?machine=&user=&worktree=, the
// same rule as `lattice list --machine/--user/--worktree`). This file only
// reads the filters from the page URL and builds that query. It is loaded as
// a plain <script> (browser globals) and is also require()-able by node for
// tests (tests/js/origin-filter.test.js).

var ORIGIN_FILTER_KEYS = ["machine", "user", "worktree"];

// An absolute POSIX worktree path normalized as the CLI's os.path.abspath
// normalizes one: repeated and trailing slashes dropped, "." and ".." folded
// (a leading "//" is kept, as POSIX allows). Anything else is returned as is:
// the page has no current directory to resolve a relative path against.
function normalizeWorktree(path) {
  var raw = String(path);
  if (raw.charAt(0) !== "/") return raw;
  var lead = raw.slice(0, 2) === "//" && raw.charAt(2) !== "/" ? "//" : "/";
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
    withOriginFilters: withOriginFilters,
  };
}
