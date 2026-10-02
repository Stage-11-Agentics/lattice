/* Pure issue queue/person logic. Kept separate from DOM and network code for node:test. */
(function (root) {
  "use strict";

  var QUEUES = [
    { key: "open", label: "No story", states: ["open"] },
    { key: "linked", label: "Has story", states: ["linked"] },
    { key: "resolved", label: "Resolved", states: ["resolved"] },
    { key: "closed", label: "Closed", states: ["dismissed", "duplicate"] }
  ];
  var EMPTY = {
    open: "Nothing needs a story. Every issue has a story, a link to one, or a reason it was closed. New issues arrive here as they are filed.",
    linked: "No issue has a story in progress.",
    resolved: "Nothing is resolved yet. An issue is resolved when every story linked to it is done.",
    closed: "Nothing has been dismissed or marked a duplicate."
  };

  function stateOf(issue) {
    return issue && typeof issue.state === "string" ? issue.state : "open";
  }

  function queueOf(issue) {
    var state = stateOf(issue);
    return state === "dismissed" || state === "duplicate" ? "closed" : state;
  }

  function commentsOf(issue) {
    return Array.isArray(issue && issue.comments) ? issue.comments : [];
  }

  function flattenComments(comments) {
    var flattened = [];
    var pending = (comments || []).slice().reverse();
    while (pending.length) {
      var comment = pending.pop();
      flattened.push(comment);
      var replies = Array.isArray(comment && comment.replies) ? comment.replies : [];
      for (var i = replies.length - 1; i >= 0; i--) pending.push(replies[i]);
    }
    return flattened;
  }

  function roundTiesToEven(value) {
    var floor = Math.floor(value);
    var fraction = value - floor;
    if (fraction < 0.5) return floor;
    if (fraction > 0.5) return floor + 1;
    return floor % 2 === 0 ? floor : floor + 1;
  }

  function frameTimes(durationMs) {
    if (durationMs < 1000) return [0];
    var count = Math.min(8, Math.max(2, 1 + Math.ceil(durationMs / 2000)));
    var span = durationMs - 100;
    var times = [];
    for (var i = 0; i < count; i++) times.push(roundTiesToEven(i * span / (count - 1)));
    return times;
  }

  function commentActor(comment) {
    return comment && (comment.author || comment.by || comment.actor) || "";
  }

  function commentTime(comment) {
    return comment && (comment.created_at || comment.at || comment.ts) || "";
  }

  function actorComments(issue, actor) {
    return flattenComments(commentsOf(issue)).filter(function (comment) {
      return commentActor(comment) === actor;
    });
  }

  function personIssues(issues, actor) {
    return (issues || []).filter(function (issue) {
      return issue.filed_by === actor || issue.matched_by || actorComments(issue, actor).length > 0;
    });
  }

  function latestActorActivity(issue, actor) {
    if (issue.actor_activity_at) return issue.actor_activity_at;
    var times = actorComments(issue, actor).map(commentTime);
    if (issue.filed_by === actor && issue.filed_at) times.push(issue.filed_at);
    return times.reduce(function (latest, value) {
      return value > latest ? value : latest;
    }, "");
  }

  function sortPersonIssues(issues, actor) {
    return personIssues(issues, actor).slice().sort(function (a, b) {
      var recent = latestActorActivity(b, actor).localeCompare(latestActorActivity(a, actor));
      if (recent) return recent;
      return (a.seq || 0) - (b.seq || 0);
    });
  }

  function personSummary(issues, actor) {
    var filed = 0;
    var comments = 0;
    var machines = Object.create(null);
    personIssues(issues, actor).forEach(function (issue) {
      if (issue.filed_by === actor) {
        filed += 1;
        addMachine(machines, actor, issue.filed_origin);
      }
      if (typeof issue.actor_comment_count === "number") {
        comments += issue.actor_comment_count;
        (issue.actor_comment_origins || []).forEach(function (origin) { addMachine(machines, actor, origin); });
      } else {
        actorComments(issue, actor).forEach(function (comment) {
          comments += 1;
          addMachine(machines, actor, comment.origin);
        });
      }
    });
    var labels = Object.keys(machines).sort();
    if (!labels.length) labels = ["unknown"];
    return { filed: filed, comments: comments, machines: labels };
  }

  function addMachine(target, actor, origin) {
    var machine = origin && origin.machine;
    var user = origin && origin.user;
    var label = !origin ? "unknown" : actor === "human:" + user ? machine : user + "@" + machine;
    target[label || "unknown"] = true;
  }

  function rowsForQueue(issues, queue, actor) {
    var rows = actor ? personIssues(issues, actor) : issues || [];
    return rows.filter(function (issue) { return queueOf(issue) === queue; });
  }

  function refreshDelta(previousRows, nextRows, selectedId) {
    var previous = Object.create(null);
    var next = Object.create(null);
    var changed = Object.create(null);
    var beforeOrder = [];
    var afterOrder = [];

    (previousRows || []).forEach(function (issue) {
      if (!issue || typeof issue.id !== "string") return;
      var key = "$" + issue.id;
      previous[key] = JSON.stringify(issue);
      beforeOrder.push(issue.id);
    });
    (nextRows || []).forEach(function (issue) {
      if (!issue || typeof issue.id !== "string") return;
      var key = "$" + issue.id;
      next[key] = JSON.stringify(issue);
      afterOrder.push(issue.id);
      if (previous[key] !== next[key]) changed[key] = issue.id;
    });
    Object.keys(previous).forEach(function (key) {
      if (!Object.prototype.hasOwnProperty.call(next, key)) changed[key] = key.slice(1);
    });
    var orderChanged = beforeOrder.length !== afterOrder.length || beforeOrder.some(function (id, index) {
      return id !== afterOrder[index];
    });
    var changedIds = Object.keys(changed).map(function (key) { return changed[key]; });
    var selectedKey = "$" + selectedId;
    var selectedWasVisible = Object.prototype.hasOwnProperty.call(previous, selectedKey);
    return {
      changedIds: changedIds,
      listChanged: orderChanged || changedIds.length > 0,
      selectedChanged: selectedId != null && selectedWasVisible && (
        !Object.prototype.hasOwnProperty.call(next, selectedKey) ||
        Object.prototype.hasOwnProperty.call(changed, selectedKey)
      )
    };
  }

  function applyRefreshDelta(previousRows, nextRows, selectedId, effects) {
    var delta = refreshDelta(previousRows, nextRows, selectedId);
    if (delta.listChanged && effects && typeof effects.updateList === "function") effects.updateList(delta);
    if (delta.selectedChanged && effects && typeof effects.updateSelected === "function") effects.updateSelected(delta);
    return delta;
  }

  function matchedBy(issue, actor) {
    if (issue.matched_by) return issue.matched_by;
    if (issue.filed_by === actor) return "filed";
    return actorComments(issue, actor).length ? "commented" : null;
  }

  var exported = {
    QUEUES: QUEUES,
    EMPTY: EMPTY,
    frameTimes: frameTimes,
    stateOf: stateOf,
    queueOf: queueOf,
    personIssues: personIssues,
    latestActorActivity: latestActorActivity,
    sortPersonIssues: sortPersonIssues,
    personSummary: personSummary,
    flattenComments: flattenComments,
    rowsForQueue: rowsForQueue,
    refreshDelta: refreshDelta,
    applyRefreshDelta: applyRefreshDelta,
    matchedBy: matchedBy,
    commentActor: commentActor,
    commentTime: commentTime
  };
  root.IssueViewLogic = exported;
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
})(typeof window !== "undefined" ? window : globalThis);
