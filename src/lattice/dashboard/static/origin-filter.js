// Origin filters for the board: machine, user and worktree (AC-39).
//
// The server does the matching (GET /api/tasks?machine=&user=&worktree=, the
// same rule as `lattice list --machine/--user/--worktree`). This file reads
// the filters from the page URL, builds that query, decides which task list
// response the page may install (createTaskListGate), and how the page's own
// selections combine with an origin-filtered list. It is loaded as
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

// The task list path for a filter snapshot: "/api/tasks" with the set
// filters as its query (none set: the bare path).
function taskListPath(filters) {
  if (originFilterCount(filters) === 0) return "/api/tasks";
  var sp = new URLSearchParams();
  ORIGIN_FILTER_KEYS.forEach(function(key) { if (filters[key]) sp.set(key, filters[key]); });
  return "/api/tasks?" + sp.toString();
}

function _snapshot(filters) {
  var snap = {};
  ORIGIN_FILTER_KEYS.forEach(function(key) { snap[key] = (filters && filters[key]) || null; });
  return snap;
}

function sameOriginFilters(a, b) {
  return ORIGIN_FILTER_KEYS.every(function(key) {
    return ((a && a[key]) || null) === ((b && b[key]) || null);
  });
}

// Which /api/tasks responses the page installs. Every request is issued with
// an explicit snapshot of the filters it asks for, and gets a ticket:
//   request()         a refresh, under the filters currently shown;
//   request(filters)  a change to *filters* (empty clears).
// accept(req) is asked when the response arrives and says whether to install
// it (a change also makes its filters the shown ones):
//   - a change installs only if no later change was requested;
//   - a refresh installs only if nothing later was installed and its filters
//     are still the shown ones (a refresh sent before a change never lands on
//     top of the change's result).
// fail(req) says whether a failed request was the latest change, so the page
// puts its inputs back to the shown filters.
function createTaskListGate(initialFilters) {
  var shown = _snapshot(initialFilters);
  var issued = 0;
  var installed = 0;
  var latestChange = 0;
  return {
    active: function() { return _snapshot(shown); },
    request: function(filters) {
      var change = filters !== undefined;
      var snap = _snapshot(change ? filters : shown);
      var ticket = ++issued;
      if (change) latestChange = ticket;
      return { ticket: ticket, change: change, filters: snap, path: taskListPath(snap) };
    },
    accept: function(req) {
      if (req.change) {
        if (req.ticket !== latestChange) return false;
        shown = _snapshot(req.filters);
      } else if (req.ticket <= installed || !sameOriginFilters(req.filters, shown)) {
        return false;
      }
      installed = Math.max(installed, req.ticket);
      return true;
    },
    fail: function(req) {
      return req.change && req.ticket === latestChange;
    },
  };
}

// A tag, assignee or creator select: the options it offers and the value it
// keeps selected, given the values present on the loaded tasks. An absent
// active value is stale and clears, unless an origin filter narrowed the
// loaded tasks (keepsAbsentSelection): then it stays selected and offered.
function resolveSelection(values, active, originFilters) {
  var options = values.slice();
  if (!active || options.indexOf(active) !== -1) return { options: options, selected: active || null };
  if (!keepsAbsentSelection(originFilters)) return { options: options, selected: null };
  options.push(active);
  return { options: options, selected: active };
}

// The page's own filters (tag, assignee, creator; AND), applied to the rows
// the server returned for the origin filters. *actorMatches* is
// actorMatchesFilter from actor-logic.js.
function taskMatchesSelections(task, selections, actorMatches) {
  if (selections.tag && (!task.tags || task.tags.indexOf(selections.tag) === -1)) return false;
  if (!actorMatches(task.assigned_to, selections.assignee)) return false;
  if (!actorMatches(task.created_by, selections.creator)) return false;
  return true;
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    ORIGIN_FILTER_KEYS: ORIGIN_FILTER_KEYS,
    normalizeWorktree: normalizeWorktree,
    originFiltersFromSearch: originFiltersFromSearch,
    originFilterCount: originFilterCount,
    keepsAbsentSelection: keepsAbsentSelection,
    taskListPath: taskListPath,
    sameOriginFilters: sameOriginFilters,
    createTaskListGate: createTaskListGate,
    resolveSelection: resolveSelection,
    taskMatchesSelections: taskMatchesSelections,
  };
}
