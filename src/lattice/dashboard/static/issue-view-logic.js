/* Pure Inbox rules: queues, the person page, formatting, filing checks and the
   refresh plan. No DOM and no network, so node:test covers every decision here. */
(function (root) {
  "use strict";

  var QUEUES = [
    { key: "open", label: "No story", states: ["open"] },
    { key: "linked", label: "Has story", states: ["linked"] },
    { key: "resolved", label: "Resolved", states: ["resolved"] },
    { key: "closed", label: "Closed", states: ["dismissed", "duplicate"] }
  ];
  var EMPTY = {
    open: "Nothing needs a story.",
    linked: "No issue has a story in progress.",
    resolved: "Nothing is resolved yet. An issue is resolved when every story linked to it is done.",
    closed: "Nothing has been dismissed or marked a duplicate.",
    person: "This person has not filed or commented on any issues."
  };
  var OPEN_DETAIL = "Every issue has a story, a link to one, or a reason it was closed. New issues arrive here as they are filed.";
  var MB = 1024 * 1024;
  var DEFAULT_LIMITS = { file: 100 * MB, issue: 250 * MB };

  function stateOf(issue) {
    return issue && typeof issue.state === "string" ? issue.state : "open";
  }

  function queueOf(issue) {
    var state = stateOf(issue);
    return state === "dismissed" || state === "duplicate" ? "closed" : state;
  }

  // The one lifecycle action the header offers: close what is live, reopen what is closed.
  function triageAction(issue) {
    if (!issue) return null;
    var queue = queueOf(issue);
    if (queue === "closed") return "reopen";
    return queue === "open" || queue === "linked" || queue === "resolved" ? "close" : null;
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

  // Frame sample times for a video of durationMs. An unknown duration (a Chrome
  // WebM reports Infinity until seeked) samples the first frame only.
  function frameTimes(durationMs) {
    if (typeof durationMs !== "number" || !isFinite(durationMs) || durationMs < 1000) return [0];
    var count = Math.min(8, Math.max(2, 1 + Math.ceil(durationMs / 2000)));
    var span = durationMs - 100;
    var times = [];
    for (var i = 0; i < count; i++) times.push(roundTiesToEven(i * span / (count - 1)));
    return times;
  }

  // A <video>'s duration in seconds as the filing payload's duration_ms, or null when unknown.
  function durationMs(seconds) {
    return typeof seconds === "number" && isFinite(seconds) && seconds > 0 ? Math.round(seconds * 1000) : null;
  }

  function commentActor(comment) {
    return comment && (comment.author || comment.by || comment.actor) || "";
  }

  function commentTime(comment) {
    return comment && (comment.created_at || comment.at || comment.ts) || "";
  }

  // ---- origins ----
  // A filing or comment origin is flattened by the server to {user, machine}; an
  // event's raw origin is {op, op_id, reported: {os_user, host}, authenticated?}.
  // A server-stamped authenticated pair wins over the reported one.
  function originPair(origin) {
    if (!origin || typeof origin !== "object") return null;
    var auth = origin.authenticated;
    var pair = null;
    if (auth && typeof auth === "object" && Object.keys(auth).length) pair = { user: auth.user, machine: auth.machine };
    else if (origin.reported && typeof origin.reported === "object") pair = { user: origin.reported.os_user, machine: origin.reported.host };
    else pair = { user: origin.user, machine: origin.machine };
    var user = typeof pair.user === "string" && pair.user ? pair.user : null;
    var machine = typeof pair.machine === "string" && pair.machine ? pair.machine : null;
    return user || machine ? { user: user, machine: machine } : null;
  }

  // "atin@Atlas"; for a human who is the machine's own user, just "Atlas".
  function originLabel(actor, origin) {
    var pair = originPair(origin);
    if (!pair) return "";
    var user = pair.user || "unknown";
    var machine = pair.machine || "unknown";
    return actor === "human:" + user ? machine : user + "@" + machine;
  }

  // ---- the person page ----
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

  // Newest activity by the person first.
  function sortPersonIssues(issues, actor) {
    return personIssues(issues, actor).slice().sort(function (a, b) {
      var recent = latestActorActivity(b, actor).localeCompare(latestActorActivity(a, actor));
      if (recent) return recent;
      return (b.seq || 0) - (a.seq || 0);
    });
  }

  function filedByPerson(issue, actor) {
    return issue.matched_by ? issue.matched_by === "filed" : issue.filed_by === actor;
  }

  // Counts and the machines the person worked from, most used first.
  function personSummary(issues, actor) {
    var filed = 0;
    var comments = 0;
    var seen = Object.create(null);
    var order = [];
    function note(origin) {
      var label = originLabel(actor, origin);
      if (!label) return;
      if (!seen[label]) { seen[label] = 0; order.push(label); }
      seen[label] += 1;
    }
    personIssues(issues, actor).forEach(function (issue) {
      if (filedByPerson(issue, actor)) {
        filed += 1;
        note(issue.filed_origin);
      }
      if (typeof issue.actor_comment_count === "number") {
        comments += issue.actor_comment_count;
        (issue.actor_comment_origins || []).forEach(note);
      } else {
        actorComments(issue, actor).forEach(function (comment) {
          comments += 1;
          note(comment.origin);
        });
      }
    });
    var machines = order.map(function (label, index) { return { label: label, index: index }; })
      .sort(function (a, b) { return seen[b.label] - seen[a.label] || a.index - b.index; })
      .map(function (item) { return item.label; });
    return { filed: filed, comments: comments, machines: machines };
  }

  function rowsForQueue(issues, queue, actor) {
    var rows = actor ? personIssues(issues, actor) : issues || [];
    return rows.filter(function (issue) { return queueOf(issue) === queue; });
  }

  function matchedBy(issue, actor) {
    if (issue.matched_by) return issue.matched_by;
    if (issue.filed_by === actor) return "filed";
    return actorComments(issue, actor).length ? "commented" : null;
  }

  // What a queue row says after its ID. Always by the issue's own state, whichever
  // list the row is in: the person page mixes every state in one list.
  function rowTag(issue, personActor) {
    var queue = queueOf(issue);
    var tag = { queue: queue, commented: !!personActor && !filedByPerson(issue, personActor) };
    if (queue === "open") {
      tag.kind = "actor";
      tag.actor = issue.filed_by;
    } else if (queue === "closed") {
      var closure = issue.closure || {};
      if (stateOf(issue) === "duplicate" || closure.kind === "duplicate") {
        tag.kind = "duplicate";
        tag.of = closure.duplicate_of || "unknown";
      } else tag.kind = "dismissed";
    } else {
      var tasks = (issue.tasks || []).filter(function (task) { return task && !task.archived && !task.erased && task.status !== "cancelled"; });
      if (!tasks.length) tasks = (issue.tasks || []).filter(Boolean);
      if (!tasks.length) {
        tag.kind = "state";
        tag.text = stateOf(issue);
      } else {
        tag.kind = "task";
        tag.task = tasks[0].short_id || tasks[0].id;
        tag.status = String(tasks[0].status || "unknown").replace(/_/g, " ");
        tag.more = tasks.length - 1;
      }
    }
    return tag;
  }

  // ---- media ----
  function liveMedia(issue) {
    return Array.isArray(issue && issue.media) ? issue.media.filter(function (item) {
      return item && !item.removed && !item.missing && item.url;
    }) : [];
  }

  // The item a queue row pictures: the first video, else the first photo.
  function rowMedia(media) {
    return (media || []).filter(function (item) { return item.kind === "video"; })[0] || (media || [])[0] || null;
  }

  // A picture for an item: a photo is itself; a video is its first stored frame,
  // or nothing (a video file is not an image source outside WebKit).
  function stillOf(item) {
    if (!item) return null;
    if (item.kind !== "video") return item.url || null;
    var frames = Array.isArray(item.frames) ? item.frames : [];
    var first = frames.filter(function (frame) { return frame && frame.url; })[0];
    return first ? first.url : null;
  }

  // Rebuild the media section only when this changes, so a playing video survives refreshes.
  function mediaKey(issueId, media) {
    return issueId + "|" + (media || []).map(function (item) {
      return [item.id, item.url, (item.frames || []).map(function (frame) { return frame.url; }).join(" ")].join(":");
    }).join(",");
  }

  // ---- formatting, as the prototype's Shell.fmt ----
  function relativeTime(timestamp, now) {
    var time = Date.parse(timestamp);
    if (!timestamp || isNaN(time)) return "—";
    var seconds = Math.max(0, Math.round(((now == null ? Date.now() : now) - time) / 1000));
    if (seconds < 60) return "just now";
    var minutes = Math.round(seconds / 60);
    if (minutes < 60) return minutes + "m ago";
    var hours = Math.round(minutes / 60);
    if (hours < 36) return hours + "h ago";
    return Math.round(hours / 24) + "d ago";
  }

  // 7.5 -> "0:07"; with tenths -> "0:07.5"
  function formatDuration(seconds, tenths) {
    if (seconds == null || typeof seconds !== "number" || !isFinite(seconds)) return tenths ? "0:00.0" : "0:00";
    seconds = Math.max(0, seconds);
    var minutes = Math.floor(seconds / 60);
    var rest = seconds - minutes * 60;
    var text = tenths ? rest.toFixed(1) : String(Math.floor(rest));
    if (rest < 10) text = "0" + text;
    return minutes + ":" + text;
  }

  function formatBytes(bytes) {
    if (bytes == null) return "—";
    if (bytes < MB) return Math.max(1, Math.round(bytes / 1024)) + " KB";
    return (bytes / MB).toFixed(1) + " MB";
  }

  function megabytes(bytes) {
    return (bytes / MB).toFixed(1).replace(/\.0$/, "") + " MB";
  }

  // ---- filing ----
  function kindOf(file) {
    var type = (file && file.type) || "";
    var name = ((file && file.name) || "").toLowerCase();
    if (/^image\//.test(type) || /\.(png|jpe?g|gif|webp|heic|avif)$/.test(name)) return "photo";
    if (/^video\//.test(type) || /\.(mp4|mov|m4v|webm|mkv)$/.test(name)) return "video";
    return null;
  }

  function mediaLimits(config) {
    var section = config && config.issues && typeof config.issues === "object" ? config.issues : {};
    function positive(value, fallback) {
      return typeof value === "number" && isFinite(value) && value > 0 && Math.floor(value) === value ? value * MB : fallback;
    }
    return { file: positive(section.max_media_mb, DEFAULT_LIMITS.file), issue: positive(section.max_issue_media_mb, DEFAULT_LIMITS.issue) };
  }

  // Why an item cannot join the tray, or null.
  function mediaProblem(acceptedBytes, item, limits) {
    limits = limits || DEFAULT_LIMITS;
    if (item.kind !== "photo" && item.kind !== "video") return "Only photos and videos can be attached.";
    if (item.bytes > limits.file) return "Too large: " + megabytes(item.bytes) + ". The limit is " + megabytes(limits.file) + " a file.";
    var total = (acceptedBytes || 0) + item.bytes;
    if (total > limits.issue) return "Too much in all: " + megabytes(total) + ". The limit is " + megabytes(limits.issue) + " for one.";
    return null;
  }

  function trayTotal(acceptedBytes, limits) {
    return formatBytes(acceptedBytes || 0) + " of " + formatBytes((limits || DEFAULT_LIMITS).issue).replace(".0 ", " ");
  }

  // Why the panel will not file yet, or null. counts: {reading, accepted, firstKind}.
  function filingProblem(title, counts) {
    if (counts.reading) return "Still reading " + (counts.reading === 1 ? "one file" : counts.reading + " files") + ". A moment.";
    if (String(title || "").trim()) return null;
    if (counts.accepted) {
      return "Add a title saying what the " + (counts.accepted === 1 ? counts.firstKind : "attachments") + " show" +
        (counts.accepted === 1 ? "s" : "") + ". Without words, a queue of screenshots cannot be scanned or searched, by a person or an agent.";
    }
    return "An issue needs a title.";
  }

  // A board where issues cannot be read here (hosted, or a bound checkout) answers LOCAL_ONLY.
  function isUnavailable(error) {
    return !!error && error.code === "LOCAL_ONLY";
  }

  // ---- refresh ----
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

  // History reads one line per thing a person did. A filing with media is one operation
  // (issue_filed plus an issue_media_added per file, sharing the origin's op_id), so it
  // reads as one line, "filed with 1 photo and 1 video"; media added later keeps its own.
  // Each returned event is the original; a filing that carried media gains filedMedia.
  function historyEntries(events) {
    var list = Array.isArray(events) ? events : [];
    var opOf = function (event) { return event && event.origin && event.origin.op_id || null; };
    var filings = Object.create(null);
    list.forEach(function (event) {
      var op = opOf(event);
      if (event && event.type === "issue_filed" && op) filings[op] = { photos: 0, videos: 0 };
    });
    var entries = [];
    list.forEach(function (event) {
      if (!event) return;
      var op = opOf(event);
      if (event.type === "issue_media_added" && op && filings[op]) {
        var kind = (event.data && (event.data.kind || event.data.media && event.data.media.kind)) || "";
        if (kind === "video") filings[op].videos++;
        else filings[op].photos++;
        return;
      }
      entries.push(event);
    });
    return entries.map(function (event) {
      var op = opOf(event);
      if (event.type !== "issue_filed" || !op || !(filings[op].photos || filings[op].videos)) return event;
      return Object.assign({}, event, { filedMedia: filings[op] });
    });
  }

  // "1 photo and 2 videos", as the prototype counts media.
  function mediaCountText(photos, videos) {
    var parts = [];
    if (photos) parts.push(photos + (photos === 1 ? " photo" : " photos"));
    if (videos) parts.push(videos + (videos === 1 ? " video" : " videos"));
    return parts.join(" and ");
  }

  // Where an issue would sit in a sorted list it is not in: how many rows sort before it.
  function slotOf(rows, issue, compare) {
    if (!issue) return 0;
    return (rows || []).filter(function (row) { return compare(row, issue) < 0; }).length;
  }

  // What one refresh does to the view. The view applies exactly this:
  //   list:   re-render the queue rows (scroll kept)
  //   cursor: the selected id and index afterwards
  //   detail: "keep" (touch nothing), "reload" (refetch, then patch only the parts
  //           that changed), or "switch" (a different issue is now selected)
  // A refresh never moves the selection off the issue being read, whatever happened to
  // it: linked, resolved, closed or out of this queue, it stays selected (and shown)
  // until the reader moves. Its index is then where it would sit (selectedSlot). Only an
  // issue gone from the board, or no selection at all, lets the cursor land on a row.
  // input: {previousRows, nextRows, selectedId, selectedIndex, selectedSlot, selectedExists}
  function planRefresh(input) {
    var delta = refreshDelta(input.previousRows, input.nextRows, input.selectedId);
    var rows = input.nextRows || [];
    var index = -1;
    for (var i = 0; i < rows.length; i++) if (rows[i].id === input.selectedId) index = i;
    var cursorId;
    var cursorIndex;
    if (index >= 0) {
      cursorId = input.selectedId;
      cursorIndex = index;
    } else if (input.selectedId != null && input.selectedExists) {
      cursorId = input.selectedId;
      cursorIndex = typeof input.selectedSlot === "number" ? input.selectedSlot : input.selectedIndex || 0;
    } else if (!rows.length) {
      cursorId = null;
      cursorIndex = 0;
    } else {
      cursorIndex = Math.max(0, Math.min(input.selectedIndex || 0, rows.length - 1));
      cursorId = rows[cursorIndex].id;
    }
    var detail = cursorId !== (input.selectedId == null ? null : input.selectedId) ? "switch" :
      delta.selectedChanged ? "reload" : "keep";
    return {
      list: delta.listChanged,
      changedIds: delta.changedIds,
      cursor: { id: cursorId, index: cursorIndex },
      detail: detail
    };
  }

  // Where j/k go from the selection. On a row, one step. Off this queue's rows (it was
  // linked, resolved or closed while being read), from the slot where it would sit: j
  // takes the row now in that slot, k the one before it.
  function stepIndex(rowCount, index, slot, delta) {
    if (!rowCount) return -1;
    var next = index >= 0 ? index + delta : delta > 0 ? slot : slot - 1;
    return Math.max(0, Math.min(rowCount - 1, next));
  }

  var exported = {
    QUEUES: QUEUES,
    EMPTY: EMPTY,
    OPEN_DETAIL: OPEN_DETAIL,
    DEFAULT_LIMITS: DEFAULT_LIMITS,
    frameTimes: frameTimes,
    durationMs: durationMs,
    stateOf: stateOf,
    queueOf: queueOf,
    triageAction: triageAction,
    originPair: originPair,
    originLabel: originLabel,
    personIssues: personIssues,
    latestActorActivity: latestActorActivity,
    sortPersonIssues: sortPersonIssues,
    personSummary: personSummary,
    flattenComments: flattenComments,
    rowsForQueue: rowsForQueue,
    rowTag: rowTag,
    liveMedia: liveMedia,
    rowMedia: rowMedia,
    stillOf: stillOf,
    mediaKey: mediaKey,
    relativeTime: relativeTime,
    formatDuration: formatDuration,
    formatBytes: formatBytes,
    kindOf: kindOf,
    mediaLimits: mediaLimits,
    mediaProblem: mediaProblem,
    trayTotal: trayTotal,
    filingProblem: filingProblem,
    isUnavailable: isUnavailable,
    refreshDelta: refreshDelta,
    historyEntries: historyEntries,
    mediaCountText: mediaCountText,
    slotOf: slotOf,
    planRefresh: planRefresh,
    stepIndex: stepIndex,
    matchedBy: matchedBy,
    commentActor: commentActor,
    commentTime: commentTime
  };
  root.IssueViewLogic = exported;
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
})(typeof window !== "undefined" ? window : globalThis);
