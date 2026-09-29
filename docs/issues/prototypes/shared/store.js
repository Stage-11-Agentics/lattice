/* In-memory issue store for the prototypes (LAT-365).
   Mirrors the LAT-361 operations and rules. Nothing is saved; a reload resets it. */
(function () {
  "use strict";

  var M = window.MOCK;
  var issues = JSON.parse(JSON.stringify(M.issues));
  var tasks = JSON.parse(JSON.stringify(M.tasks));
  var nextTaskSeq = M.next_task_seq;
  var listeners = [];
  var history = {};
  var clock = Date.parse(M.now);

  // The operator's limits (LAT-366): 100 MB a file, 250 MB an issue. The real CLI shrinks video first when ffmpeg
  // is present, so these bite rarely; the prototype stores files as given.
  var LIMITS = { file: 100 * 1000 * 1000, issue: 250 * 1000 * 1000 }; // decimal MB, as file browsers show them
  function mb(n) { return (n / 1e6).toFixed(1).replace(/\.0$/, "") + " MB"; }
  function mediaSummary(list) {
    var p = 0, v = 0;
    (list || []).forEach(function (m) { if (m.kind === "video") { v++; } else { p++; } });
    return p || v ? { photos: p, videos: v } : {};
  }
  // Why an item cannot join a set of items, or null. Used by the store and by the filing tray.
  function mediaProblem(existing, item) {
    if (item.kind !== "photo" && item.kind !== "video") { return "Only photos and videos can be attached."; }
    if (item.bytes > LIMITS.file) { return "Too large: " + mb(item.bytes) + ". The limit is " + mb(LIMITS.file) + " a file."; }
    var total = (existing || []).reduce(function (a, m) { return a + (m.bytes || 0); }, 0);
    if (total + item.bytes > LIMITS.issue) { return "Too much in all: " + mb(total + item.bytes) + ". The limit is " + mb(LIMITS.issue) + " for one."; }
    return null;
  }
  var mediaSeq = 0;
  function newMedia(item, by, at) {
    var m = {};
    Object.keys(item).forEach(function (k) { m[k] = item[k]; });
    m.id = "med_new_" + (++mediaSeq);
    m.frames = m.frames || [];
    m.added_by = by; m.added_at = at;
    return m;
  }

  function tick() { clock += 1000; return new Date(clock).toISOString().replace(/\.\d+Z$/, "Z"); }
  function emit() { listeners.forEach(function (fn) { fn(); }); }
  function fail(code, message) { var e = new Error(message); e.code = code; throw e; }

  function log(id, type, by, data, at) {
    (history[id] = history[id] || []).push({ type: type, by: by, at: at, data: data || {} });
  }

  // Seed each issue's history from its mock fields, so the detail views have a trail to show.
  issues.forEach(function (i) {
    i.media = i.media || [];
    log(i.id, "issue_filed", i.filed_by, mediaSummary(i.media), i.filed_at);
    i.links.forEach(function (l) { log(i.id, "issue_linked", l.linked_by, { task_id: l.task_id }, l.linked_at); });
    if (i.closure) {
      log(i.id, i.closure.kind === "dismissed" ? "issue_dismissed" : "issue_marked_duplicate", i.closure.by,
        i.closure.kind === "dismissed" ? { reason: i.closure.reason } : { duplicate_of: i.closure.duplicate_of }, i.closure.at);
    }
    history[i.id].sort(function (a, b) { return a.at < b.at ? -1 : 1; });
  });

  function get(id) {
    var found = null;
    issues.forEach(function (i) { if (i.id === id) { found = i; } });
    return found;
  }
  function task(id) {
    var found = null;
    tasks.forEach(function (t) { if (t.id === id) { found = t; } });
    return found;
  }
  function need(id) { var i = get(id); if (!i) { fail("NOT_FOUND", "No issue " + id + "."); } return i; }

  function liveLinks(issue) {
    return issue.links.filter(function (l) { var t = task(l.task_id); return t && t.status !== "cancelled"; });
  }

  // open | linked | resolved | dismissed | duplicate. Never stored.
  function state(issue) {
    if (issue.closure) { return issue.closure.kind; }
    var live = liveLinks(issue);
    if (!live.length) { return "open"; }
    var allDone = live.every(function (l) { return task(l.task_id).status === "done"; });
    return allDone ? "resolved" : "linked";
  }

  function counts() {
    var c = { open: 0, linked: 0, resolved: 0, dismissed: 0, duplicate: 0, total: issues.length };
    issues.forEach(function (i) { c[state(i)] += 1; });
    return c;
  }

  function issuesFor(taskId) {
    return issues.filter(function (i) { return i.links.some(function (l) { return l.task_id === taskId; }); });
  }

  function file(p) {
    var text = (p.text || "").trim();
    if (!text) { fail("VALIDATION_ERROR", "An issue needs some text."); }
    var incoming = p.media || [], kept = [];
    incoming.forEach(function (m) {
      var why = mediaProblem(kept, m);
      if (why) { fail("VALIDATION_ERROR", why); }
      kept.push(m);
    });
    var seq = issues.reduce(function (m, i) { return Math.max(m, i.seq); }, 0) + 1;
    var issue = {
      seq: seq, id: M.project_code + "-I" + seq, text: text, confidence: p.confidence || null,
      evidence: p.evidence || [], source: p.source || null, filed_by: p.by || M.me, filed_at: tick(),
      links: [], closure: null, media: []
    };
    issue.media = kept.map(function (m) { return newMedia(m, issue.filed_by, issue.filed_at); });
    issues.push(issue);
    log(issue.id, "issue_filed", issue.filed_by, mediaSummary(issue.media), issue.filed_at);
    emit();
    return issue;
  }

  function link(id, taskId, by) {
    var i = need(id);
    if (i.closure) { fail("CONFLICT", i.id + " is closed. Reopen it first."); }
    if (!task(taskId)) { fail("NOT_FOUND", "No story " + taskId + "."); }
    if (i.links.some(function (l) { return l.task_id === taskId; })) { return { idempotent: true }; }
    var at = tick();
    i.links.push({ task_id: taskId, linked_by: by || M.me, linked_at: at });
    log(i.id, "issue_linked", by || M.me, { task_id: taskId }, at);
    emit();
    return { idempotent: false };
  }

  function unlink(id, taskId, by) {
    var i = need(id);
    var before = i.links.length;
    i.links = i.links.filter(function (l) { return l.task_id !== taskId; });
    if (i.links.length !== before) { log(i.id, "issue_unlinked", by || M.me, { task_id: taskId }, tick()); emit(); }
  }

  function firstLine(text) {
    var line = text.split("\n")[0].trim();
    return line.length > 120 ? line.slice(0, 117) + "..." : line;
  }

  function promote(ids, p) {
    p = p || {};
    var picked = ids.map(need);
    picked.forEach(function (i) { if (i.closure) { fail("CONFLICT", i.id + " is closed. Reopen it first."); } });
    var t = {
      id: M.project_code + "-" + (nextTaskSeq++), title: (p.title || firstLine(picked[0].text)).trim(),
      status: "backlog", priority: p.priority || "medium", type: p.type || "bug", assigned: null, created_here: true
    };
    tasks.push(t);
    picked.forEach(function (i) {
      var at = tick();
      i.links.push({ task_id: t.id, linked_by: M.me, linked_at: at });
      log(i.id, "issue_linked", M.me, { task_id: t.id, promoted: true }, at);
    });
    emit();
    return t;
  }

  function dismiss(id, reason) {
    var i = need(id);
    if (i.closure) { fail("CONFLICT", i.id + " is already closed. Reopen it first."); }
    if (!(reason || "").trim()) { fail("VALIDATION_ERROR", "Dismissing needs a reason."); }
    var at = tick();
    i.closure = { kind: "dismissed", reason: reason.trim(), by: M.me, at: at };
    log(i.id, "issue_dismissed", M.me, { reason: reason.trim() }, at);
    emit();
  }

  function duplicate(id, ofId) {
    var i = need(id), target = need(ofId);
    if (i.closure) { fail("CONFLICT", i.id + " is already closed. Reopen it first."); }
    if (i.id === target.id) { fail("VALIDATION_ERROR", "An issue cannot be a duplicate of itself."); }
    if (target.closure && target.closure.kind === "duplicate") {
      fail("VALIDATION_ERROR", target.id + " is itself a duplicate of " + target.closure.duplicate_of + ". Point at that one.");
    }
    var at = tick();
    i.closure = { kind: "duplicate", duplicate_of: target.id, by: M.me, at: at };
    log(i.id, "issue_marked_duplicate", M.me, { duplicate_of: target.id }, at);
    emit();
  }

  function reopen(id) {
    var i = need(id);
    if (!i.closure) { fail("CONFLICT", i.id + " is not closed."); }
    i.closure = null;
    log(i.id, "issue_reopened", M.me, {}, tick());
    emit();
  }

  // Add a photo or video to an issue after it was filed. Allowed on closed issues too: it is evidence.
  function addMedia(id, item, by) {
    var i = need(id);
    var why = mediaProblem(i.media, item);
    if (why) { fail("VALIDATION_ERROR", why); }
    var at = tick(), m = newMedia(item, by || M.me, at);
    i.media.push(m);
    log(i.id, "issue_media_added", by || M.me, { media_id: m.id, kind: m.kind }, at);
    emit();
    return m;
  }

  // Remove one item. A reason is required. The history says that something was removed, by whom and why,
  // and keeps neither the file nor its name: a screenshot with a secret in it is the case this exists for.
  function removeMedia(id, mediaId, reason, by) {
    var i = need(id);
    if (!(reason || "").trim()) { fail("VALIDATION_ERROR", "Removing needs a reason."); }
    var found = null;
    i.media.forEach(function (m) { if (m.id === mediaId) { found = m; } });
    if (!found) { fail("NOT_FOUND", "No such photo or video on " + i.id + "."); }
    i.media = i.media.filter(function (m) { return m !== found; });
    if (found.local && found.src && window.URL && URL.revokeObjectURL) { try { URL.revokeObjectURL(found.src); } catch (e) { /* ignore */ } }
    log(i.id, "issue_media_removed", by || M.me, { kind: found.kind, reason: reason.trim() }, tick());
    emit();
  }

  // Prototype-only: lets a viewer move a story along to watch issue states follow it.
  function setTaskStatus(taskId, status) {
    var t = task(taskId);
    if (t) { t.status = status; emit(); }
  }

  window.IssueStore = {
    now: function () { return clock; },
    me: M.me,
    projectCode: M.project_code,
    issues: function () { return issues.slice().sort(function (a, b) { return a.seq - b.seq; }); },
    tasks: function () { return tasks.slice(); },
    get: get, task: task, state: state, counts: counts, issuesFor: issuesFor, liveLinks: liveLinks,
    history: function (id) { return (history[id] || []).slice(); },
    duplicatesOf: function (id) { return issues.filter(function (i) { return i.closure && i.closure.duplicate_of === id; }); },
    file: file, link: link, unlink: unlink, promote: promote, dismiss: dismiss, duplicate: duplicate, reopen: reopen,
    setTaskStatus: setTaskStatus,
    addMedia: addMedia, removeMedia: removeMedia, mediaProblem: mediaProblem, LIMITS: LIMITS, mb: mb,
    refresh: emit,
    subscribe: function (fn) { listeners.push(fn); },
    STATES: ["open", "linked", "resolved", "dismissed", "duplicate"],
    TASK_STATUSES: ["backlog", "in_planning", "planned", "in_progress", "review", "in_validation", "pr_open", "done", "blocked", "cancelled"]
  };
})();
