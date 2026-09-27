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

// A board value as a CSS class token: lowercase letters, digits, "_" and "-"
// only (whitespace becomes "_", anything else "-"). A class attribute is never
// built from a raw board value, even an escaped one.
function classToken(s) {
  return String(s == null ? "" : s)
    .toLowerCase()
    .replace(/\s+/g, "_")
    .replace(/[^a-z0-9_-]/g, "-");
}

// `map[key]` only when `key` is the map's own property, so a board value such
// as "constructor" or "__proto__" never reaches Object.prototype.
function ownValue(map, key) {
  if (map == null || typeof map !== "object") return undefined;
  return Object.prototype.hasOwnProperty.call(map, key) ? map[key] : undefined;
}

// A status's display name: the workflow's display_names entry when it is a
// string, else the slug with "_" read as spaces. Plain text: escape it to render.
function statusDisplayName(workflow, slug) {
  if (!slug) return "";
  var name = ownValue(workflow && workflow.display_names, slug);
  if (typeof name === "string" && name) return name;
  return String(slug).replace(/_/g, " ");
}

// One legend row: a coloured mark and a label, every value escaped.
function legendItemHtml(itemClass, markClass, color, label) {
  return '<div class="' + esc(itemClass) + '">'
    + '<span class="' + esc(markClass) + '" style="background:' + esc(color) + '"></span>'
    + '<span>' + esc(label) + '</span></div>';
}

// The opening tag of a board lane for `status`: its class from classToken, the
// raw status only as an escaped data attribute.
function boardColumnOpenTag(status, empty) {
  return '<div class="board-col status-' + classToken(status) + (empty ? " empty-col" : "")
    + '" data-status="' + esc(status) + '">';
}

// --- Status markup ---
// Every place a workflow status, its display name, its description, or its
// lane colour renders goes through one of these builders, which escape every
// value. The node tests run a hostile corpus through each of them.

// A status label in a <span>: `opts.background` / `opts.color` become style
// declarations (then `opts.style`), `opts.title` the tooltip.
function statusSpanHtml(cls, label, opts) {
  opts = opts || {};
  var style = "";
  if (opts.background != null) style += "background:" + opts.background + ";";
  if (opts.color != null) style += "color:" + opts.color + ";";
  if (opts.style) style += opts.style;
  return '<span class="' + esc(cls) + '"'
    + (style ? ' style="' + esc(style) + '"' : "")
    + (opts.title != null ? ' title="' + esc(opts.title) + '"' : "")
    + ">" + esc(label) + "</span>";
}

// One <option> of a status select.
function statusOptionHtml(value, label, selected) {
  return '<option value="' + esc(value) + '"' + (selected ? " selected" : "") + ">"
    + esc(label) + "</option>";
}

// A board card's opening tag. `classes` are the page's own class names (heat
// tier, needs-human); the task's id, status, and heat timestamp are escaped
// data attributes.
function boardCardOpenTag(task, classes) {
  var html = '<div class="' + esc(["card"].concat(classes || []).join(" ")) + '" draggable="true"'
    + ' data-task-id="' + esc(task.id) + '" data-task-status="' + esc(task.status || "") + '"';
  if (task.last_status_changed_at) html += ' data-heat-ts="' + esc(task.last_status_changed_at) + '"';
  return html + ">";
}

// A task's status select: its current status (selected), then each target.
function statusSelectOptionsHtml(workflow, current, targets) {
  var html = statusOptionHtml(current, statusDisplayName(workflow, current), true);
  (targets || []).forEach(function(s) {
    html += statusOptionHtml(s, statusDisplayName(workflow, s), false);
  });
  return html;
}

// A board lane's header: lane colour, description tooltip, label, card count.
function boardColumnHeaderHtml(color, description, label, count) {
  return '<div class="board-col-header" style="background:' + esc(color)
    + ';color:var(--text-on-lane)" title="' + esc(description) + '">'
    + "<span>" + esc(label) + "</span>"
    + '<span class="count">' + esc(count) + "</span></div>";
}

// A lane's organisation dropdown, opening tag.
function laneSortSelectOpenTag(status) {
  return '<select class="lane-sort" data-status="' + esc(status)
    + '" title="How cards in this lane are organized">';
}

// One row of the settings panel's lane colours.
function laneColorRowHtml(status, color) {
  return '<div class="lane-color-row"><span class="lane-name">' + esc(status) + "</span>"
    + '<input type="color" data-status="' + esc(status) + '" value="' + esc(color) + '"></div>';
}

// One bar of a statistics chart: label, fill width and colour, count.
function statsBarRowHtml(label, pct, color, count, countTitle) {
  return '<div class="stats-bar-row"><span class="stats-bar-label">' + esc(label) + "</span>"
    + '<div class="stats-bar-track"><div class="stats-bar-fill" style="width:' + esc(pct)
    + "%;background:" + esc(color) + '"></div></div>'
    + '<span class="stats-bar-count"' + (countTitle != null ? ' title="' + esc(countTitle) + '"' : "")
    + ">" + esc(count) + "</span></div>";
}

// The statistics view's work-in-progress alert for one status.
function wipAlertHtml(prefix, status, current, limit) {
  return '<div class="stats-wip-alert">' + esc(prefix) + ": <strong>" + esc(status)
    + "</strong> &mdash; " + esc(current) + "/" + esc(limit) + "</div>";
}

// The Web view tooltip's status row: a lane-coloured dot and the status.
function webStatusRowHtml(color, status) {
  return '<div style="display:flex;gap:6px;align-items:center;margin-bottom:2px">'
    + '<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:'
    + esc(color) + '"></span>'
    + '<span style="color:var(--text-muted);font-size:11px;min-width:50px">Status</span>'
    + "<span>" + esc(status) + "</span></div>";
}

// A status change as escaped text: "<from> → <to>" by display name.
function statusTransitionHtml(workflow, from, to) {
  return esc(statusDisplayName(workflow, from || "?") + " → " + statusDisplayName(workflow, to || "?"));
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
  module.exports = {
    esc: esc,
    classToken: classToken,
    ownValue: ownValue,
    statusDisplayName: statusDisplayName,
    legendItemHtml: legendItemHtml,
    boardColumnOpenTag: boardColumnOpenTag,
    statusSpanHtml: statusSpanHtml,
    boardCardOpenTag: boardCardOpenTag,
    statusOptionHtml: statusOptionHtml,
    statusSelectOptionsHtml: statusSelectOptionsHtml,
    boardColumnHeaderHtml: boardColumnHeaderHtml,
    laneSortSelectOpenTag: laneSortSelectOpenTag,
    laneColorRowHtml: laneColorRowHtml,
    statsBarRowHtml: statsBarRowHtml,
    wipAlertHtml: wipAlertHtml,
    webStatusRowHtml: webStatusRowHtml,
    statusTransitionHtml: statusTransitionHtml,
    basePath: basePath,
    apiUrl: apiUrl
  };
}
