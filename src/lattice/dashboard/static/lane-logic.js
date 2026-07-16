"use strict";

// --- Per-lane organization (sort + group) ---
// Pure lane sort/group logic for the dashboard board. Extracted from index.html's
// IIFE so it has a real test home (tests/js/lane-logic.test.js runs under node:test,
// bridged into pytest). This file is a classic browser script loaded WITHOUT defer
// before the inline IIFE, so these names are globals by the time the IIFE runs; the
// CommonJS export guard at the bottom lets node require it. Keep it ES5-flavored (var,
// function expressions) to match the monolith it came from.
//
// Each lane carries its own mode, stored as dashboard.lane_sort[status]. An absent
// entry means DEFAULT_LANE_SORT, which reproduces the board's original ULID ordering.
var DEFAULT_LANE_SORT = "created_asc";

var LANE_SORT_MODES = [
  {value: "created_asc",     label: "Oldest first",      group: "Sort"},
  {value: "created_desc",    label: "Newest first",      group: "Sort"},
  {value: "priority",        label: "Priority",          group: "Sort"},
  {value: "urgency",         label: "Urgency",           group: "Sort"},
  {value: "updated_desc",    label: "Recently updated",  group: "Sort"},
  {value: "status_age_desc", label: "Time in status",    group: "Sort"},
  {value: "complexity_asc",  label: "Quick wins",        group: "Sort"},
  {value: "group:assignee",  label: "Assignee",          group: "Group"},
  {value: "group:tag",       label: "Tag",               group: "Group"}
];

// Done-lane-only modes, kept in sync with the legacy done_display setting.
var DONE_LANE_MODES = [
  {value: "group:day", label: "Day completed", group: "Group"},
  {value: "recent",    label: "Recent 24h",    group: "Group"}
];

var PRIORITY_RANK   = {critical: 0, high: 1, medium: 2, low: 3};
var URGENCY_RANK    = {immediate: 0, high: 1, normal: 2, low: 3};
var COMPLEXITY_RANK = {low: 0, medium: 1, high: 2};

// done_display is the pre-existing setting for the done lane. Map it onto lane modes
// so an existing config keeps rendering exactly as it did before this feature.
var DONE_DISPLAY_TO_MODE = {grouped: "group:day", recent: "recent", all: "created_asc"};
var MODE_TO_DONE_DISPLAY = {"group:day": "grouped", recent: "recent"};

function laneModesFor(status) {
  return status === "done" ? LANE_SORT_MODES.concat(DONE_LANE_MODES) : LANE_SORT_MODES;
}

// The server validates lane_sort as a str→str map but does not police the values (a
// hand-edited config.json can hold anything). Everything downstream indexes lookup
// tables with this string, so it gets checked against the lane's own mode list here —
// the single point where a mode enters the app.
function isValidLaneMode(status, mode) {
  return laneModesFor(status).some(function(m) { return m.value === mode; });
}

// Pure: the config-independent core of getLaneSort. Given the raw lane_sort map and the
// resolved legacy done_display, decide which mode a lane renders in. A valid explicit
// mode wins; the done lane falls back to deriving from done_display; everything else
// falls back to the default. The IIFE's getLaneSort is a thin wrapper that gathers the
// config values and delegates here, keeping the legacy-derivation rules testable without
// stubbing globals.
function resolveLaneSort(status, laneSortMap, doneDisplay) {
  var mode = laneSortMap && laneSortMap[status];
  if (mode && isValidLaneMode(status, mode)) return mode;
  if (status === "done") {
    var legacy = doneDisplay || "grouped";
    var derived = Object.prototype.hasOwnProperty.call(DONE_DISPLAY_TO_MODE, legacy)
      ? DONE_DISPLAY_TO_MODE[legacy]
      : null;
    return derived && isValidLaneMode(status, derived) ? derived : DEFAULT_LANE_SORT;
  }
  return DEFAULT_LANE_SORT;
}

// Rank-based comparator over an enum field; unset values sort last.
function _byRank(field, ranks) {
  return function(a, b) {
    var ra = ranks[a[field]], rb = ranks[b[field]];
    if (ra == null) ra = 99;
    if (rb == null) rb = 99;
    return ra - rb;
  };
}

// Timestamp comparator. If either side is missing the field, compare both by id instead
// — ULIDs are monotonic in creation time, but they are not lexicographically comparable
// with ISO timestamps, so the fallback has to apply to both operands or neither.
function _byTime(field, dir) {
  return function(a, b) {
    var va = a[field], vb = b[field];
    if (!va || !vb) {
      va = a.id || "";
      vb = b.id || "";
    }
    if (va === vb) return 0;
    return (va < vb ? -1 : 1) * dir;
  };
}

var LANE_COMPARATORS = {
  created_asc:  _byTime("created_at", 1),
  created_desc: _byTime("created_at", -1),
  updated_desc: _byTime("updated_at", -1),
  // Oldest transition = longest sitting in this lane, so age-descending is timestamp-ascending.
  status_age_desc: _byTime("last_status_changed_at", 1),
  priority:     _byRank("priority", PRIORITY_RANK),
  urgency:      _byRank("urgency", URGENCY_RANK),
  complexity_asc: _byRank("complexity", COMPLEXITY_RANK)
};

// Pure: returns a new sorted array. Ties always break on id, so renders are stable.
function sortLaneItems(items, mode) {
  // hasOwnProperty, not a truthiness check: LANE_COMPARATORS["constructor"] would
  // otherwise hand back an inherited function and silently sort nothing.
  var cmp = Object.prototype.hasOwnProperty.call(LANE_COMPARATORS, mode)
    ? LANE_COMPARATORS[mode]
    : LANE_COMPARATORS[DEFAULT_LANE_SORT];
  return items.slice().sort(function(a, b) {
    return cmp(a, b) || ((a.id || "") < (b.id || "") ? -1 : (a.id || "") > (b.id || "") ? 1 : 0);
  });
}

// Pure: returns [{label, items}] sections. A card belongs to exactly one section — cards
// are never duplicated, so the lane's header count stays honest. Multi-tag cards group
// under their alphabetically-first tag.
function groupLaneItems(items, mode) {
  var key, emptyLabel;
  if (mode === "group:assignee") {
    key = function(t) { return t.assigned_to || ""; };
    emptyLabel = "Unassigned";
  } else {
    key = function(t) {
      var tags = (t.tags || []).slice().sort();
      return tags.length ? tags[0] : "";
    };
    emptyLabel = "Untagged";
  }

  // Object.create(null): tags and actor ids are free-form strings, so a task tagged
  // "constructor" or "__proto__" would otherwise hit an inherited property, skip the
  // array init, and throw inside renderBoard() — blanking the entire board, not just
  // this lane.
  var buckets = Object.create(null);
  items.forEach(function(t) {
    var k = key(t);
    if (!buckets[k]) buckets[k] = [];
    buckets[k].push(t);
  });

  // Named sections alphabetically; the empty bucket always sinks to the bottom.
  var names = Object.keys(buckets).filter(function(k) { return k !== ""; }).sort();
  if (buckets[""]) names.push("");

  return names.map(function(k) {
    return {
      label: k === "" ? emptyLabel : k,
      items: sortLaneItems(buckets[k], DEFAULT_LANE_SORT)
    };
  });
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { DEFAULT_LANE_SORT, LANE_SORT_MODES, DONE_LANE_MODES,
    laneModesFor, isValidLaneMode, resolveLaneSort, sortLaneItems, groupLaneItems,
    DONE_DISPLAY_TO_MODE, MODE_TO_DONE_DISPLAY, LANE_COMPARATORS };
}
