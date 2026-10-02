/* Round-four Inbox UI. Network calls and DOM rendering stay out of issue-view-logic.js. */
(function (root) {
  "use strict";

  function mount(options) {
    var app = options.app;
    var api = options.api;
    var apiPost = options.apiPost;
    var esc = options.esc;
    var hosted = options.hosted;
    var logic = root.IssueViewLogic;
    var queues = logic.QUEUES;
    var issues = [];
    var personRows = [];
    var details = Object.create(null);
    var queue = "open";
    var person = null;
    var cursors = Object.create(null);
    var personBack = null;
    var historyOpen = false;
    var shownId = null;
    var mediaKey = null;
    var focusMedia = 0;
    var closer = null;
    var copyTimer = null;
    var requestGeneration = 0;
    var active = false;
    var loaded = false;
    var loading = false;
    var fileState = null;
    var recorder = null;
    var recorderStream = null;
    var destroyed = false;

    function actorText(actor) {
      return typeof actor === "string" ? actor : (root.actorTooltip ? root.actorTooltip(actor) : String(actor || ""));
    }
    function actorName(actor) {
      return root.actorDisplayName ? root.actorDisplayName(actor) : actorText(actor);
    }
    function originLabel(actor, origin) {
      if (!origin) return "";
      var user = origin.user || "unknown";
      var machine = origin.machine || "unknown";
      return actorText(actor) === "human:" + user ? machine : user + "@" + machine;
    }
    function originHtml(actor, origin) {
      var label = originLabel(actor, origin);
      return label ? ' <span class="issue-origin">· ' + esc(label) + "</span>" : "";
    }
    function actorHtml(actor, origin) {
      var full = actorText(actor);
      if (!full) return "unknown";
      return '<span class="actor issue-actor" data-actor="' + esc(full) + '" title="' + esc(full) + '">' +
        esc(actorName(actor)) + "</span>" + originHtml(actor, origin);
    }
    function displayTime(timestamp) {
      if (!timestamp) return "unknown time";
      var date = new Date(timestamp);
      if (isNaN(date.getTime())) return String(timestamp);
      return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
    }
    function relativeTime(timestamp) {
      if (!timestamp) return "—";
      var date = new Date(timestamp);
      if (isNaN(date.getTime())) return "—";
      var seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
      if (seconds < 60) return "now";
      if (seconds < 3600) return Math.floor(seconds / 60) + "m";
      if (seconds < 86400) return Math.floor(seconds / 3600) + "h";
      if (seconds < 86400 * 30) return Math.floor(seconds / 86400) + "d";
      if (seconds < 86400 * 365) return Math.floor(seconds / (86400 * 30)) + "mo";
      return Math.floor(seconds / (86400 * 365)) + "y";
    }
    function formatDuration(seconds, precise) {
      seconds = Math.max(0, Math.floor(Number(seconds) || 0));
      var minutes = Math.floor(seconds / 60);
      var remain = seconds % 60;
      return precise ? minutes + ":" + String(remain).padStart(2, "0") :
        (minutes ? minutes + ":" + String(remain).padStart(2, "0") : remain + "s");
    }
    function issueById(id, source) {
      var rows = source || (person ? personRows : issues);
      return rows.find(function (issue) { return issue.id === id; }) || details[id] || null;
    }
    function visibleRows() {
      return person ? personRows : issues;
    }
    function rowsForQueue(key) {
      if (person && key === "all") return logic.sortPersonIssues(personRows, person.actor);
      return logic.rowsForQueue(visibleRows(), key, person ? person.actor : null)
        .sort(function (a, b) {
          if (person) return logic.latestActorActivity(b, person.actor).localeCompare(logic.latestActorActivity(a, person.actor)) ||
            (a.seq || 0) - (b.seq || 0);
          return (a.seq || 0) - (b.seq || 0);
        });
    }
    function currentKey() { return (person ? "p:" + person.actor + ":" : "") + queue; }
    function current() { return issueById(cursors[currentKey()]); }
    function setCursor(issue) {
      if (issue) cursors[currentKey()] = issue.id;
      else delete cursors[currentKey()];
    }
    function settleCursor(rows) {
      var key = currentKey();
      var id = cursors[key];
      var index = rows.findIndex(function (issue) { return issue.id === id; });
      if (index < 0) index = Math.min(cursors[key + ":index"] || 0, rows.length - 1);
      if (!rows.length) {
        delete cursors[key];
        cursors[key + ":index"] = 0;
      } else {
        index = Math.max(0, index);
        cursors[key] = rows[index].id;
        cursors[key + ":index"] = index;
      }
    }
    function mountFrame() {
      app.innerHTML = '<div class="issue-inbox">' +
        '<section class="issue-q-pane" aria-label="Issue queue">' +
          '<div class="issue-q-tabs" id="issue-q-tabs"></div>' +
          '<div class="issue-person-head" id="issue-person-head"></div>' +
          '<div class="issue-q-list" id="issue-q-list" role="listbox" aria-label="Issues"></div>' +
          '<div class="issue-q-foot"><kbd>j</kbd><kbd>k</kbd> move<span class="sep"></span><kbd>c</kbd> copy ID<span class="sep"></span><kbd>f</kbd> look closer' +
            (hosted ? "" : '<span class="sep"></span><kbd>i</kbd> file') + "</div>" +
        "</section>" +
        '<section class="issue-d-pane" aria-label="Selected issue">' +
          '<div class="issue-d-head" id="issue-d-head"><button class="issue-id-copy" id="issue-id-copy" title="Copy the ID (c), to tell an agent what to do with it" disabled></button>' +
            '<span class="issue-copy-status" id="issue-copy-status"></span><span id="issue-d-state"></span><span class="issue-d-facts" id="issue-d-facts"></span></div>' +
          '<div class="issue-d-body" id="issue-d-body"></div>' +
        "</section></div>";
      var tabs = document.getElementById("issue-q-tabs");
      tabs.innerHTML = queues.map(function (item, index) {
        return '<button type="button" class="issue-q-tab" data-q="' + esc(item.key) + '" aria-pressed="false">' +
          '<kbd>' + (index + 1) + '</kbd><span class="issue-q-label">' + esc(item.label) + '</span>' +
          '<span class="issue-q-count" id="issue-qc-' + esc(item.key) + '">0</span></button>';
      }).join("");
      tabs.addEventListener("click", function (event) {
        var button = event.target.closest("[data-q]");
        if (button) switchQueue(button.getAttribute("data-q"));
      });
      document.getElementById("issue-q-list").addEventListener("click", function (event) {
        var actor = event.target.closest("[data-actor]");
        if (actor) { enterPerson(actor.getAttribute("data-actor")); return; }
        var row = event.target.closest("[data-issue-id]");
        if (!row) return;
        setCursor(issueById(row.getAttribute("data-issue-id")));
        render();
      });
      document.getElementById("issue-id-copy").addEventListener("click", copyId);
      document.getElementById("issue-d-head").addEventListener("click", function (event) {
        var actor = event.target.closest("[data-actor]");
        if (actor) enterPerson(actor.getAttribute("data-actor"));
      });
      document.getElementById("issue-d-body").addEventListener("click", onDetailClick);
      document.getElementById("issue-d-body").addEventListener("keydown", function (event) {
        if (event.target.id === "issue-comment-box" && event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
          event.preventDefault(); postComment();
        }
      });
    }
    function setActive(active) {
      document.body.classList.toggle("issue-view-active", !!active);
      if (active) {
        var nav = document.querySelector(".nav");
        if (nav) document.documentElement.style.setProperty("--issue-nav-height", nav.offsetHeight + "px");
      }
    }
    function showHostedUnavailable() {
      setActive(false);
      app.innerHTML = '<section class="issue-unavailable"><h1>Issues</h1><p>Issues are not available on this board yet.</p></section>';
    }
    async function loadIssues() {
      if (hosted || destroyed) return;
      loading = true;
      var generation = ++requestGeneration;
      try {
        var rows = await api("/api/issues");
        if (destroyed || generation !== requestGeneration) return;
        issues = Array.isArray(rows) ? rows : [];
        details = Object.create(null);
        if (person) await loadPersonIssues(person.actor, generation);
        loading = false;
        loaded = true;
        render();
      } catch (error) {
        if (generation === requestGeneration) {
          loading = false;
          loaded = true;
          showLoadError(error);
        }
      }
    }
    async function loadPersonIssues(actor, parentGeneration) {
      if (hosted || destroyed) return;
      var rows = await api("/api/issues?by=" + encodeURIComponent(actor));
      if (destroyed || (parentGeneration != null && parentGeneration !== requestGeneration)) return;
      personRows = Array.isArray(rows) ? logic.sortPersonIssues(rows, actor) : [];
    }
    function showLoadError(error) {
      var list = document.getElementById("issue-q-list");
      if (list) list.innerHTML = '<div class="issue-q-empty">Could not load issues: ' + esc(error.message || error) + "</div>";
      var body = document.getElementById("issue-d-body");
      if (body) body.innerHTML = '<div class="issue-quiet">Refresh to try again.</div>';
    }
    function refresh() {
      if (hosted) { showHostedUnavailable(); return Promise.resolve(); }
      active = true;
      setActive(true);
      loaded = false;
      return loadIssues();
    }
    function render() {
      if (destroyed) return;
      if (hosted) { showHostedUnavailable(); return; }
      active = true;
      setActive(true);
      var tabs = document.getElementById("issue-q-tabs");
      if (!tabs) {
        mountFrame();
        tabs = document.getElementById("issue-q-tabs");
      }
      if (!loaded && !loading) loadIssues();
      queues.forEach(function (item) {
        var count = person ? logic.rowsForQueue(personRows, item.key, person.actor).length :
          issues.filter(function (issue) { return logic.queueOf(issue) === item.key; }).length;
        var countEl = document.getElementById("issue-qc-" + item.key);
        if (countEl) countEl.textContent = String(count);
        var tab = tabs.querySelector('[data-q="' + item.key + '"]');
        if (tab) tab.setAttribute("aria-pressed", queue === item.key ? "true" : "false");
      });
      var rows = rowsForQueue(queue);
      settleCursor(rows);
      renderPersonHead();
      if (loading && !issues.length && !personRows.length) {
        document.getElementById("issue-q-list").innerHTML = '<div class="issue-q-empty">Loading issues…</div>';
      } else renderQueue(rows);
      renderDetail(current());
      if (closer && (!current() || closer.issue !== current().id)) {
        if (current() && mediaOf(current()).length) openCloser(0);
        else closeCloser();
      }
    }
    function renderPersonHead() {
      var head = document.getElementById("issue-person-head");
      head.classList.toggle("on", !!person);
      if (!person) {
        head.innerHTML = '<span>Click a name to see everything from that person.</span>';
        return;
      }
      var summary = logic.personSummary(personRows, person.actor);
      head.innerHTML = '<div class="issue-person-line"><span class="actor">' + esc(actorName(person.actor)) + '</span>' +
        '<span class="issue-person-counts">' + summary.filed + " filed · " + summary.comments +
        (summary.comments === 1 ? " comment" : " comments") + '</span></div><div class="issue-person-line">' +
        '<span class="issue-person-machines">from ' + esc(summary.machines.join(", ") || "unknown") + '</span>' +
        '<button type="button" class="btn" data-action="leave-person" title="Back to all issues (Esc)"><kbd>Esc</kbd> All</button></div>';
    }
    function renderQueue(rows) {
      var list = document.getElementById("issue-q-list");
      if (!rows.length) {
        list.innerHTML = '<div class="issue-q-empty">' + esc(emptyMessage()) + "</div>";
        return;
      }
      var selected = cursors[currentKey()];
      list.innerHTML = rows.map(function (issue) {
        var rowMedia = mediaOf(issue);
        var first = rowMedia.find(function (media) { return media.kind === "video"; }) || rowMedia[0];
        var thumbnail = first && first.url ? '<img loading="lazy" decoding="async" src="' + esc(root.apiUrl(options.basePath, first.url)) + '" alt="">' : "";
        var tag = tagFor(issue);
        var commentCount = Number(issue.comment_count || 0);
        var mediaCount = rowMedia.length;
        var thumb = !mediaCount ? '<span class="issue-q-thumb none"></span>' :
          '<span class="issue-q-thumb">' + thumbnail +
          (first.kind === "video" ? '<span class="issue-q-badge issue-q-video">▶ ' + esc(formatDuration((first.duration_ms || 0) / 1000)) + "</span>" : "") +
          (mediaCount > 1 ? '<span class="issue-q-badge issue-q-count-badge">' + mediaCount + "</span>" : "") + "</span>";
        return '<div class="issue-q-row' + (issue.id === selected ? " cursor" : "") + '" data-issue-id="' + esc(issue.id) +
          '" role="option" aria-selected="' + (issue.id === selected ? "true" : "false") + '"><div class="issue-q-main"><div class="issue-q-top">' +
          '<span class="issue-id">' + esc(issue.short_id || issue.id) + '</span><span class="issue-q-tag">' + tag + '</span>' +
          '<span class="issue-q-comments" title="' + commentCount + (commentCount === 1 ? " comment" : " comments") + '">' +
          (commentCount ? '&#9679; ' + commentCount : "") + '</span><span class="issue-q-age" title="' + esc(displayTime(issue.filed_at)) + '">' +
          esc(relativeTime(issue.filed_at)) + '</span></div><div class="issue-q-text" title="' + esc(issue.title || "") + '">' +
          esc(issue.title || "Untitled issue") + "</div></div>" + thumb + "</div>";
      }).join("");
      var selectedRow = list.querySelector(".cursor");
      if (selectedRow) selectedRow.scrollIntoView({ block: "nearest" });
    }
    function tagFor(issue) {
      var why = person && issue.filed_by !== person.actor ? '<span class="issue-q-why">commented</span> · ' : "";
      if (queue === "open") return why + actorHtml(issue.filed_by, issue.filed_origin);
      if (queue === "closed") {
        var closure = issue.closure || {};
        return why + (closure.kind === "duplicate" ? "duplicate of " + esc(closure.duplicate_of || "unknown") : "dismissed");
      }
      var tasks = issue.tasks || [];
      var first = tasks.find(function (task) { return !task.archived && !task.erased; }) || tasks[0];
      if (!first) return why + esc(queue === "resolved" ? "resolved" : "has story");
      return why + '<span class="task-id">' + esc(first.short_id || first.id) + "</span> " + esc(String(first.status || "unknown").replace(/_/g, " ")) +
        (tasks.length > 1 ? " +" + (tasks.length - 1) : "");
    }
    function mediaOf(issue) { return Array.isArray(issue && issue.media) ? issue.media.filter(function (item) { return !item.removed && !item.missing && item.url; }) : []; }
    function currentDetail() {
      var issue = current();
      if (!issue) return null;
      return details[issue.id] || issue;
    }
    async function ensureDetail(issue) {
      if (!issue || details[issue.id] || hosted) return;
      var id = issue.id;
      try {
        var detail = await api("/api/issues/" + encodeURIComponent(id));
        if (destroyed || current() && current().id !== id) return;
        details[id] = detail;
        renderDetail(current());
      } catch (error) {
        var body = document.getElementById("issue-d-body");
        if (body && current() && current().id === id) body.innerHTML = '<div class="issue-quiet">Could not load issue: ' + esc(error.message || error) + "</div>";
      }
    }
    function stateBadge(state) {
      var color = state === "resolved" ? "#22c55e" : state === "open" ? "#f59e0b" : "#8b8fa3";
      return '<span class="badge" style="background:' + color + ';color:#fff">' + esc(state || "open") + "</span>";
    }
    function factsHtml(issue) {
      var bits = [actorHtml(issue.filed_by, issue.filed_origin), '<span title="' + esc(displayTime(issue.filed_at)) + '">' +
        esc(relativeTime(issue.filed_at)) + "</span>"];
      if (issue.source) bits.push(esc(issue.source));
      if (issue.confidence) bits.push(esc(issue.confidence));
      (issue.evidence || []).forEach(function (item) { bits.push(esc(item)); });
      return bits.join(" · ");
    }
    function detailShell() {
      return '<h1 class="issue-d-title" id="issue-d-title"></h1><div class="issue-d-desc" id="issue-d-desc"></div>' +
        '<div class="issue-d-media" id="issue-d-media"></div><div class="issue-outcome" id="issue-outcome"></div>' +
        '<div class="issue-meta" id="issue-meta"></div><div class="issue-comments" id="issue-comments"></div>' +
        '<div class="issue-compose" id="issue-compose"><textarea id="issue-comment-box" rows="1" placeholder="Comment, or ask an agent: @claude-opus-impl ..."></textarea>' +
        '<button type="button" class="btn btn-sm" id="issue-comment-post" title="Post (⌘ Enter)">Post</button></div><div id="issue-history"></div>';
    }
    function renderDetail(issue) {
      var body = document.getElementById("issue-d-body");
      var id = issue ? issue.id : "none:" + queue;
      var changed = id !== shownId;
      shownId = id;
      var copy = document.getElementById("issue-id-copy");
      copy.textContent = issue ? issue.short_id || issue.id : "";
      copy.disabled = !issue;
      document.getElementById("issue-d-state").innerHTML = issue ? stateBadge(logic.stateOf(issue)) : "";
      document.getElementById("issue-d-facts").innerHTML = issue ? factsHtml(issue) : "";
      var status = document.getElementById("issue-copy-status");
      if (changed && !status.classList.contains("done")) status.textContent = issue ? "copy" : "";
      if (!issue) {
        mediaKey = null;
        var message = emptyMessage();
        body.innerHTML = '<div class="issue-quiet"><h2>' + esc(message.split(".")[0]) + "</h2>" + esc(message) + "</div>";
        return;
      }
      if (!document.getElementById("issue-d-title")) {
        body.innerHTML = detailShell();
        document.getElementById("issue-comment-post").addEventListener("mousedown", function (event) { event.preventDefault(); });
        document.getElementById("issue-comment-post").addEventListener("click", postComment);
        var commentBox = document.getElementById("issue-comment-box");
        commentBox.addEventListener("focus", function () { document.getElementById("issue-compose").classList.add("active"); });
        commentBox.addEventListener("blur", function () { if (!commentBox.value.trim()) document.getElementById("issue-compose").classList.remove("active"); });
        commentBox.addEventListener("keydown", function (event) { if (event.key === "Escape") { event.preventDefault(); commentBox.blur(); } });
      }
      var detail = details[issue.id];
      if (!detail) {
        body.innerHTML = '<div class="issue-quiet">Loading issue…</div>';
        ensureDetail(issue);
        return;
      }
      var scrollTop = changed ? 0 : body.scrollTop;
      document.getElementById("issue-d-title").textContent = detail.title || issue.title || "Untitled issue";
      document.getElementById("issue-d-desc").textContent = detail.description || "";
      document.getElementById("issue-comments").innerHTML = commentsHtml(detail);
      if (changed) {
        document.getElementById("issue-comment-box").value = "";
        document.getElementById("issue-compose").classList.remove("active");
      }
      renderMedia(detail);
      document.getElementById("issue-outcome").innerHTML = outcomeHtml(detail);
      document.getElementById("issue-meta").innerHTML = metaHtml(detail);
      renderHistory(detail);
      body.scrollTop = scrollTop;
    }
    function emptyMessage() {
      if (person && queue === "all") return "This person has not filed or commented on any issues.";
      return logic.EMPTY[queue] || "No issues are in this queue.";
    }
    function metaHtml(issue) {
      var bits = [];
      if (issue.filed_at) bits.push("Filed " + displayTime(issue.filed_at));
      if (issue.updated_at) bits.push("Updated " + displayTime(issue.updated_at));
      return bits.length ? '<div class="issue-meta-line">' + bits.map(esc).join(" · ") + "</div>" : "";
    }
    function outcomeHtml(issue) {
      if (issue.closure) {
        var closure = issue.closure;
        if (closure.kind === "duplicate") {
          var duplicate = closure.duplicate_of || "unknown";
          var target = (issues || []).find(function (item) { return item.short_id === duplicate || item.id === duplicate; });
          return '<div class="issue-closure"><b>Duplicate of <a href="#" data-goto="' + esc(target ? target.id : duplicate) + '">' + esc(duplicate) +
            "</a>.</b> " + esc(target ? target.title : "") + "</div>";
        }
        return '<div class="issue-closure"><b>Dismissed.</b> ' + esc(closure.reason || "") + "</div>";
      }
      var tasks = issue.tasks || [];
      if (!tasks.length) return '<div class="issue-no-story">No story is linked. An agent can create one from this issue.</div>';
      return tasks.map(function (task) {
        var title = task.title || (task.erased ? "Erased task" : "Task unavailable");
        return '<div class="issue-story' + (task.status === "cancelled" || task.erased ? " dead" : "") + '"><span class="task-id">' +
          esc(task.short_id || task.id) + '</span><span class="badge">' + esc(String(task.status || "unknown").replace(/_/g, " ")) +
          '</span><span class="issue-story-title" title="' + esc(title) + '">' + esc(title) + "</span></div>";
      }).join("");
    }
    function commentBody(text) {
      return esc(text).replace(/(^|\s)(@[\w:.-]+)/g, '$1<span class="mention">$2</span>')
        .replace(/\bLAT-I\d+\b/g, function (value) { return '<span class="iid">' + value + "</span>"; })
        .replace(/\bLAT-\d+\b/g, function (value) { return '<span class="tid">' + value + "</span>"; });
    }
    function commentsHtml(issue) {
      var comments = logic.flattenComments(issue.comments || []);
      if (!comments.length) return "";
      return '<div class="issue-comments-head">Comments (' + comments.length + ")</div>" + comments.map(function (comment) {
        var author = comment.author || comment.by || comment.actor || "unknown";
        var time = comment.created_at || comment.at || comment.ts;
        var kind = String(author).indexOf("human:") === 0 ? "human" : "agent";
        return '<div class="issue-comment issue-comment-' + kind + '"><div class="issue-comment-meta">' + actorHtml(author, comment.origin) +
          '<span title="' + esc(displayTime(time)) + '">' + esc(relativeTime(time)) + '</span></div><div class="issue-comment-body">' +
          commentBody(comment.body || "") + "</div></div>";
      }).join("");
    }
    function postComment() {
      var issue = current();
      var box = document.getElementById("issue-comment-box");
      if (!issue || !box || !box.value.trim()) return;
      var body = box.value;
      apiPost("/api/issues/" + encodeURIComponent(issue.id) + "/comment", { body: body }).then(function () {
        box.value = "";
        box.focus();
        refresh();
      }).catch(function (error) { options.showToast(error.message || String(error), "error"); });
    }
    function eventHistoryLine(event) {
      var data = event.data || {};
      var what;
      switch (event.type) {
        case "issue_filed": what = "filed"; break;
        case "issue_linked": what = (data.promoted ? "made story " : "linked to ") + (data.task_id || "task"); break;
        case "issue_unlinked": what = "unlinked from " + (data.task_id || "task"); break;
        case "issue_dismissed": what = "dismissed: " + (data.reason || ""); break;
        case "issue_marked_duplicate": what = "marked a duplicate of " + (data.duplicate_of || "issue"); break;
        case "issue_reopened": what = "reopened"; break;
        case "issue_media_added": what = "added a " + (data.kind || "file"); break;
        case "issue_comment_added": what = "commented"; break;
        case "issue_media_removed": what = "removed a " + (data.kind || "file") + (data.reason ? ": " + data.reason : ""); break;
        default: what = event.type || "changed";
      }
      var actor = event.actor || event.by || "unknown";
      return '<div><span class="issue-history-at" title="' + esc(displayTime(event.ts || event.at)) + '">' + esc(relativeTime(event.ts || event.at)) +
        '</span><span class="issue-history-what">' + actorHtml(actor, event.origin ? event.origin : data.origin) + " " + esc(what) + "</span></div>";
    }
    function renderHistory(issue) {
      var history = issue.events || [];
      var box = document.getElementById("issue-history");
      if (!box) return;
      box.innerHTML = '<button type="button" class="issue-history-toggle" aria-expanded="' + historyOpen + '"><span class="caret">' +
        (historyOpen ? "▾" : "▸") + "</span>History (" + history.length + ")</button>" +
        (historyOpen ? '<div class="issue-history">' + history.map(eventHistoryLine).join("") + "</div>" : "");
    }
    function thumbSource(media) { return media.kind === "video" ? (media.poster_url || media.url) : media.url; }
    function frameSeconds(frame) { return Number(frame.t_ms || 0) / 1000; }
    function renderMedia(issue) {
      var box = document.getElementById("issue-d-media");
      if (!box) return;
      var media = mediaOf(issue);
      var key = issue.id + "|" + media.map(function (item) { return item.id; }).join(",");
      if (key === mediaKey) return;
      mediaKey = key;
      focusMedia = 0;
      box.innerHTML = media.map(function (item, index) {
        var src = root.apiUrl(options.basePath, item.url);
        var name = item.original_name || item.name || "attached media";
        var width = item.width || 1280;
        var height = item.height || 800;
        var content = item.kind === "video" ? '<video src="' + esc(src) + '" muted playsinline preload="metadata" title="' + esc(name) + '"></video>' +
          '<button type="button" class="issue-media-play" data-play="' + index + '" title="Play (muted)">▶</button>' :
          '<img src="' + esc(src) + '" alt="photo attached to ' + esc(issue.short_id || issue.id) + '" title="' + esc(name) + '">';
        var frames = item.kind === "video" ? (item.frames || []).map(function (frame, frameIndex) {
          return '<button type="button" class="issue-af-frame" data-frame="' + frameIndex + '" data-item="' + index + '"><img src="' +
            esc(root.apiUrl(options.basePath, frame.url)) + '" alt="frame at ' + esc(formatDuration(frameSeconds(frame), true)) + '" loading="lazy"><span>' +
            esc(formatDuration(frameSeconds(frame), true)) + "</span></button>";
        }).join("") : "";
        var agentFrames = item.kind === "video" ? '<div class="issue-af">' + (frames ?
          '<div class="issue-af-label">What an agent sees: these frames, taken when it was filed. Click one to see that moment.</div><div class="issue-af-row">' + frames + "</div>" :
          '<div class="issue-af-none">Frames are made when this is filed.</div>') + "</div>" : "";
        return '<figure class="issue-media-item" data-item="' + index + '"><div class="issue-media-frame ' + esc(item.kind || "photo") + '" data-w="' +
          esc(width) + '" data-h="' + esc(height) + '">' + content + "</div>" + agentFrames + "</figure>";
      }).join("");
      box.querySelectorAll("video").forEach(function (video) {
        var figure = video.closest(".issue-media-item");
        var item = media[Number(figure.getAttribute("data-item"))];
        video.addEventListener("timeupdate", function () {
          var currentFrame = -1;
          (item.frames || []).forEach(function (frame, index) { if (frameSeconds(frame) <= video.currentTime + 0.05) currentFrame = index; });
          figure.querySelectorAll(".issue-af-frame").forEach(function (frame, index) { frame.classList.toggle("on", index === currentFrame); });
        });
      });
      sizeMedia();
    }
    function sizeMedia() {
      var box = document.getElementById("issue-d-media");
      if (!box) return;
      var maxWidth = box.clientWidth - 2;
      var maxHeight = Math.round(window.innerHeight * 0.72);
      box.querySelectorAll(".issue-media-frame").forEach(function (frame) {
        var width = Number(frame.getAttribute("data-w")) || 1280;
        var height = Number(frame.getAttribute("data-h")) || 800;
        var scale = Math.min(1, maxWidth / width, maxHeight / height);
        frame.style.width = Math.round(width * scale) + 2 + "px";
        frame.style.height = Math.round(height * scale) + 2 + "px";
      });
    }
    function playInline(index) {
      var figure = document.querySelector('.issue-media-item[data-item="' + index + '"]');
      var video = figure && figure.querySelector("video");
      if (!video) return;
      video.controls = true;
      figure.querySelector(".issue-media-frame").classList.add("playing");
      var play = video.play();
      if (play && play.catch) play.catch(function () {});
    }
    function seekFrame(itemIndex, frameIndex) {
      var figure = document.querySelector('.issue-media-item[data-item="' + itemIndex + '"]');
      var video = figure && figure.querySelector("video");
      var media = mediaOf(currentDetail())[itemIndex];
      if (!video || !media || !media.frames || !media.frames[frameIndex]) return;
      video.pause();
      video.controls = true;
      figure.querySelector(".issue-media-frame").classList.add("playing");
      video.currentTime = frameSeconds(media.frames[frameIndex]);
      figure.querySelectorAll(".issue-af-frame").forEach(function (frame, index) { frame.classList.toggle("on", index === frameIndex); });
      focusMedia = itemIndex;
    }
    function openCloser(index) {
      var issue = currentDetail();
      var media = mediaOf(issue);
      if (!media.length) return;
      index = Math.max(0, Math.min(media.length - 1, Number(index) || 0));
      var item = media[index];
      var inline = document.querySelector('.issue-media-item[data-item="' + index + '"] video');
      var start = inline ? inline.currentTime : 0;
      var wasPlaying = inline && !inline.paused;
      if (inline) inline.pause();
      var overlay = document.getElementById("issue-closer");
      if (!overlay) {
        overlay = document.createElement("div");
        overlay.id = "issue-closer";
        overlay.className = "issue-closer";
        document.body.appendChild(overlay);
      }
      var src = root.apiUrl(options.basePath, item.url);
      var title = issue.title || "Issue";
      overlay.innerHTML = '<div class="issue-closer-bar"><span class="issue-id">' + esc(issue.short_id || issue.id) +
        '</span><span class="issue-closer-title">' + esc(title) + '</span><span class="issue-closer-pos">' +
        (media.length > 1 ? (index + 1) + " of " + media.length : "") + '</span><span class="issue-closer-keys">' +
        (media.length > 1 ? "<kbd>←</kbd> <kbd>→</kbd> item &nbsp; " : "") + "<kbd>j</kbd> <kbd>k</kbd> issue &nbsp; <kbd>f</kbd> or <kbd>Esc</kbd> closes</span></div>" +
        '<div class="issue-closer-media" data-close>' + (item.kind === "video" ? '<video src="' + esc(src) + '" muted playsinline controls></video>' :
          '<img src="' + esc(src) + '" alt="">') + "</div>";
      var video = overlay.querySelector("video");
      if (video) {
        video.currentTime = start;
        if (wasPlaying) { var play = video.play(); if (play && play.catch) play.catch(function () {}); }
      }
      closer = { index: index, issue: issue.id };
    }
    function closeCloser() {
      var overlay = document.getElementById("issue-closer");
      if (overlay) {
        var video = overlay.querySelector("video");
        var inline = closer && document.querySelector('.issue-media-item[data-item="' + closer.index + '"] video');
        if (video && inline) inline.currentTime = video.currentTime;
        overlay.remove();
      }
      closer = null;
    }
    function onDetailClick(event) {
      var actor = event.target.closest("[data-actor]");
      if (actor) { enterPerson(actor.getAttribute("data-actor")); return; }
      var play = event.target.closest("[data-play]");
      if (play) { playInline(Number(play.getAttribute("data-play"))); return; }
      var frame = event.target.closest("[data-frame]");
      if (frame) { seekFrame(Number(frame.getAttribute("data-item")), Number(frame.getAttribute("data-frame"))); return; }
      if (event.target.closest(".issue-history-toggle")) { historyOpen = !historyOpen; renderHistory(currentDetail()); return; }
      var goto = event.target.closest("[data-goto]");
      if (goto) {
        event.preventDefault();
        var target = goto.getAttribute("data-goto");
        var issue = issues.find(function (item) { return item.id === target || item.short_id === target; });
        if (issue) goTo(issue.id);
        return;
      }
      var figure = event.target.closest(".issue-media-item");
      if (figure) {
        var index = Number(figure.getAttribute("data-item"));
        focusMedia = index;
        if (event.target.closest(".issue-media-frame.photo")) openCloser(index);
        else if (event.target.closest(".issue-media-frame.video:not(.playing)")) playInline(index);
      }
      if (event.target.closest("[data-action=leave-person]")) leavePerson();
    }
    function onKeydown(event) {
      if (destroyed || hosted) return;
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      var tag = (event.target.tagName || "").toLowerCase();
      var dialog = document.getElementById("issue-file-dialog");
      if (dialog) {
        if (event.key === "Escape") {
          closeFileDialog();
          event.preventDefault();
        }
        return;
      }
      if (document.body.classList.contains("issue-view-active") === false) return;
      if (tag === "input" || tag === "textarea" || tag === "select" || event.target.isContentEditable) return;
      var key = event.key;
      var handled = true;
      if (key === "j" || key === "ArrowDown") move(1);
      else if (key === "k" || key === "ArrowUp") move(-1);
      else if (key === "c") copyId();
      else if (key === "i") openFileDialog();
      else if (key === "f") { if (closer) closeCloser(); else if (mediaOf(currentDetail()).length) openCloser(focusMedia); }
      else if (key === "Escape") { if (closer) closeCloser(); else if (person) leavePerson(); else handled = false; }
      else if (closer && key === "ArrowRight") openCloser(closer.index + 1);
      else if (closer && key === "ArrowLeft") openCloser(closer.index - 1);
      else if (key === " ") {
        var inline = document.querySelector("#issue-d-media video");
        var large = document.querySelector("#issue-closer video");
        var video = large || inline;
        if (!video) handled = false;
        else if (video.paused) {
          if (!large) playInline(Number(video.closest(".issue-media-item").getAttribute("data-item")));
          else { var play = video.play(); if (play && play.catch) play.catch(function () {}); }
        } else video.pause();
      } else if (/^[1-4]$/.test(key)) switchQueue(queues[Number(key) - 1].key);
      else handled = false;
      if (handled) event.preventDefault();
    }
    function move(delta) {
      var rows = rowsForQueue(queue);
      if (!rows.length) return;
      var key = currentKey();
      var index = rows.findIndex(function (issue) { return issue.id === cursors[key]; });
      index = Math.max(0, Math.min(rows.length - 1, (index < 0 ? 0 : index) + delta));
      cursors[key] = rows[index].id;
      cursors[key + ":index"] = index;
      render();
    }
    function switchQueue(key) {
      queue = person && queue === key ? "all" : key;
      render();
    }
    function goTo(id) {
      var issue = issueById(id, issues);
      if (!issue) return;
      person = null;
      personRows = [];
      queue = logic.queueOf(issue);
      cursors[currentKey()] = issue.id;
      render();
    }
    function enterPerson(actor) {
      if (!actor || person && person.actor === actor) return;
      if (!person) personBack = { queue: queue, id: cursors[currentKey()] };
      person = { actor: actor };
      queue = "all";
      personRows = [];
      render();
      var generation = ++requestGeneration;
      loadPersonIssues(actor, generation).then(function () {
        if (person && person.actor === actor && generation === requestGeneration) {
          var rows = rowsForQueue(queue);
          var currentId = personBack && personBack.id;
          if (currentId && rows.some(function (item) { return item.id === currentId; })) cursors[currentKey()] = currentId;
          else if (rows[0]) cursors[currentKey()] = rows[0].id;
          render();
        }
      }).catch(function (error) { showLoadError(error); });
    }
    function leavePerson() {
      if (!person) return;
      person = null;
      personRows = [];
      queue = personBack ? personBack.queue : "open";
      if (personBack && personBack.id) cursors[currentKey()] = personBack.id;
      personBack = null;
      render();
    }
    function copyId() {
      var issue = current();
      if (!issue) return;
      var status = document.getElementById("issue-copy-status");
      function show(ok) {
        status.textContent = ok ? "Copied" : "Not copied";
        status.classList.toggle("done", ok);
        clearTimeout(copyTimer);
        copyTimer = setTimeout(function () { status.textContent = current() ? "copy" : ""; status.classList.remove("done"); }, 1600);
      }
      function fallback() {
        var area = document.createElement("textarea");
        area.value = issue.short_id || issue.id;
        area.setAttribute("readonly", ""); area.style.position = "fixed"; area.style.opacity = "0";
        document.body.appendChild(area); area.select();
        var copied = false;
        try { copied = document.execCommand("copy"); } catch (_error) { copied = false; }
        area.remove();
        return copied;
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(issue.short_id || issue.id).then(function () { show(true); }, function () { show(fallback()); });
      } else show(fallback());
    }

    function openFileDialog() {
      if (hosted || document.getElementById("issue-file-dialog")) return;
      fileState = { files: [], previewUrls: [], busy: false, status: "", camera: false };
      var overlay = document.createElement("div");
      overlay.className = "issue-file-overlay";
      overlay.id = "issue-file-dialog";
      overlay.innerHTML = '<section class="issue-file-dialog" role="dialog" aria-modal="true" aria-labelledby="issue-file-title">' +
        '<header class="issue-file-head"><h2 id="issue-file-title">File an issue</h2><button type="button" class="btn issue-file-close" data-file-action="close" aria-label="Close">×</button></header>' +
        '<div class="issue-file-body"><label>Title<input id="issue-file-title-input" type="text" maxlength="120" placeholder="One clear sentence" autocomplete="off"></label>' +
        '<label>Description (optional)<textarea id="issue-file-description" placeholder="What happened? What did you expect?"></textarea></label>' +
        '<div class="issue-file-tools"><button type="button" class="btn" data-file-action="choose">Choose files</button>' +
        '<button type="button" class="btn" data-file-action="record">Record video</button><input id="issue-file-input" type="file" accept="image/png,image/jpeg,image/gif,image/webp,video/mp4,video/quicktime,video/webm" multiple>' +
        '<span class="issue-file-hint">Paste or drop photos and videos here. Videos get frames an agent can read.</span></div>' +
        '<div class="issue-file-tray" id="issue-file-tray"></div></div><footer class="issue-file-foot"><span class="issue-file-status" id="issue-file-status">Paste, drop, choose or record.</span>' +
        '<button type="button" class="btn issue-file-submit" data-file-action="submit">File issue</button></footer></section>';
      document.body.appendChild(overlay);
      overlay.addEventListener("click", onFileClick);
      overlay.addEventListener("change", onFileChange);
      overlay.addEventListener("dragover", function (event) { event.preventDefault(); });
      overlay.addEventListener("drop", function (event) { event.preventDefault(); addFiles(event.dataTransfer.files); });
      overlay.addEventListener("paste", function (event) { addFiles(event.clipboardData && event.clipboardData.files); });
      document.getElementById("issue-file-title-input").focus();
      renderFileTray();
    }
    function onFileClick(event) {
      var action = event.target.closest("[data-file-action]");
      if (!action) return;
      var name = action.getAttribute("data-file-action");
      if (name === "close") closeFileDialog();
      else if (name === "choose") document.getElementById("issue-file-input").click();
      else if (name === "record") toggleRecording();
      else if (name === "submit") submitFile();
      else if (name === "remove") removeFile(Number(action.getAttribute("data-index")));
    }
    function onFileChange(event) {
      if (event.target.id === "issue-file-input") addFiles(event.target.files);
    }
    function addFiles(list) {
      if (!fileState || !list) return;
      Array.prototype.forEach.call(list, function (file) {
        if (file && typeof file.arrayBuffer === "function") fileState.files.push(file);
      });
      var input = document.getElementById("issue-file-input");
      if (input) input.value = "";
      renderFileTray();
    }
    function removeFile(index) {
      if (!fileState) return;
      fileState.files.splice(index, 1);
      renderFileTray();
    }
    function renderFileTray() {
      var tray = document.getElementById("issue-file-tray");
      if (!tray || !fileState) return;
      fileState.previewUrls.forEach(function (url) { URL.revokeObjectURL(url); });
      fileState.previewUrls = [];
      tray.innerHTML = fileState.files.map(function (file, index) {
        var preview = "";
        if (file.type && (file.type.indexOf("image/") === 0 || file.type.indexOf("video/") === 0)) {
          var previewUrl = URL.createObjectURL(file);
          fileState.previewUrls.push(previewUrl);
          preview = file.type.indexOf("image/") === 0 ? '<img src="' + esc(previewUrl) + '" alt="">' :
            '<video src="' + esc(previewUrl) + '" muted></video>';
        }
        return '<div class="issue-file-item">' + preview + '<button type="button" class="issue-file-remove" data-file-action="remove" data-index="' +
          index + '" aria-label="Remove file">×</button><div class="issue-file-item-name" title="' + esc(file.name) + '">' + esc(file.name) + '</div>' +
          esc(formatFileSize(file.size)) + "</div>";
      }).join("");
      var status = document.getElementById("issue-file-status");
      if (status) status.textContent = fileState.status || (fileState.files.length ? fileState.files.length + (fileState.files.length === 1 ? " file" : " files") + " ready." : "Paste, drop, choose or record.");
    }
    function formatFileSize(bytes) {
      if (bytes < 1024) return bytes + " B";
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
      return (bytes / (1024 * 1024)).toFixed(1) + " MB";
    }
    function closeFileDialog() {
      var overlay = document.getElementById("issue-file-dialog");
      if (recorder && recorder.state !== "inactive") recorder.stop();
      if (recorderStream) recorderStream.getTracks().forEach(function (track) { track.stop(); });
      recorder = null;
      recorderStream = null;
      if (overlay) overlay.remove();
      if (fileState) fileState.previewUrls.forEach(function (url) { URL.revokeObjectURL(url); });
      fileState = null;
    }
    async function toggleRecording() {
      if (!fileState) return;
      if (recorder && recorder.state !== "inactive") {
        recorder.stop();
        return;
      }
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === "undefined") {
        fileState.status = "Video recording is not available in this browser."; renderFileTray(); return;
      }
      try {
        recorderStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
        if (!fileState) {
          recorderStream.getTracks().forEach(function (track) { track.stop(); });
          recorderStream = null;
          return;
        }
        var mime = MediaRecorder.isTypeSupported("video/mp4") ? "video/mp4" : "video/webm";
        recorder = new MediaRecorder(recorderStream, { mimeType: mime });
        var chunks = [];
        recorder.ondataavailable = function (event) { if (event.data && event.data.size) chunks.push(event.data); };
        recorder.onstop = function () {
          if (chunks.length && fileState) {
            var extension = mime.indexOf("mp4") >= 0 ? "mp4" : "webm";
            addFiles([new File(chunks, "Issue recording." + extension, { type: mime })]);
          }
          if (recorderStream) recorderStream.getTracks().forEach(function (track) { track.stop(); });
          recorder = null; recorderStream = null;
          if (fileState) { fileState.status = "Recording added."; renderFileTray(); }
        };
        recorder.start();
        fileState.status = "Recording… select Record video again to stop.";
        renderFileTray();
      } catch (error) {
        fileState.status = error.message || "Could not start recording.";
        renderFileTray();
      }
    }
    function bytesToBase64(buffer) {
      var bytes = new Uint8Array(buffer);
      var binary = "";
      var chunkSize = 0x8000;
      for (var start = 0; start < bytes.length; start += chunkSize) {
        binary += String.fromCharCode.apply(null, bytes.subarray(start, Math.min(start + chunkSize, bytes.length)));
      }
      return btoa(binary);
    }
    async function payloadFromBlob(blob, filename) {
      var buffer = await blob.arrayBuffer();
      var hash = await crypto.subtle.digest("SHA-256", buffer);
      var digest = Array.prototype.map.call(new Uint8Array(hash), function (byte) { return byte.toString(16).padStart(2, "0"); }).join("");
      return { filename: filename, content_b64: bytesToBase64(buffer), sha256: digest };
    }
    function waitFor(target, name) {
      return new Promise(function (resolve, reject) {
        function cleanup() {
          target.removeEventListener(name, ready);
          target.removeEventListener("error", failed);
        }
        function ready(event) { cleanup(); resolve(event); }
        function failed() { cleanup(); reject(new Error("Could not read video metadata")); }
        target.addEventListener(name, ready, { once: true });
        target.addEventListener("error", failed, { once: true });
      });
    }
    async function videoFrames(file, duration) {
      var objectUrl = URL.createObjectURL(file);
      var video = document.createElement("video");
      video.muted = true; video.playsInline = true; video.preload = "auto";
      try {
        var metadata = waitFor(video, "loadedmetadata");
        var firstFrame = waitFor(video, "loadeddata");
        video.src = objectUrl;
        await Promise.all([metadata, firstFrame]);
        var frames = [];
        var sampleTimes = logic.frameTimes(Math.round(duration * 1000));
        for (var i = 0; i < sampleTimes.length; i++) {
          var tMs = sampleTimes[i];
          var seconds = tMs / 1000;
          if (frames.length && frames[frames.length - 1].t_ms === tMs) continue;
          if (Math.abs(video.currentTime - seconds) > 0.001) {
            var seeked = waitFor(video, "seeked");
            video.currentTime = seconds;
            await seeked;
          }
          var scale = Math.min(1, 1568 / Math.max(video.videoWidth, video.videoHeight));
          var canvas = document.createElement("canvas");
          canvas.width = Math.max(1, Math.round(video.videoWidth * scale));
          canvas.height = Math.max(1, Math.round(video.videoHeight * scale));
          canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
          var frameBlob = await new Promise(function (resolve) { canvas.toBlob(resolve, "image/jpeg", 0.82); });
          if (frameBlob) frames.push({ t_ms: tMs, payload: await payloadFromBlob(frameBlob, "frame.jpg") });
        }
        return frames;
      } finally {
        video.removeAttribute("src");
        URL.revokeObjectURL(objectUrl);
      }
    }
    async function mediaFromFile(file) {
      var payload = await payloadFromBlob(file, file.name || "attachment");
      if (!file.type || file.type.indexOf("video/") !== 0) return { payload: payload };
      var objectUrl = URL.createObjectURL(file);
      var video = document.createElement("video");
      video.preload = "metadata"; video.muted = true;
      try {
        var metadata = waitFor(video, "loadedmetadata");
        video.src = objectUrl;
        await metadata;
        var width = video.videoWidth || 0;
        var height = video.videoHeight || 0;
        var durationMs = Math.max(0, Math.round((video.duration || 0) * 1000));
        var frames = await videoFrames(file, video.duration || 0);
        return { payload: payload, video: { width: width, height: height, duration_ms: durationMs }, frames: frames };
      } finally {
        video.removeAttribute("src");
        URL.revokeObjectURL(objectUrl);
      }
    }
    async function submitFile() {
      if (!fileState || fileState.busy) return;
      var title = document.getElementById("issue-file-title-input").value.trim();
      var description = document.getElementById("issue-file-description").value;
      if (!title) { document.getElementById("issue-file-title-input").focus(); return; }
      fileState.busy = true;
      fileState.status = "Preparing media…";
      renderFileTray();
      var submit = document.querySelector("[data-file-action=submit]");
      if (submit) submit.disabled = true;
      try {
        var media = [];
        for (var i = 0; i < fileState.files.length; i++) media.push(await mediaFromFile(fileState.files[i]));
        var result = await apiPost("/api/issues", { title: title, description: description, media: media });
        closeFileDialog();
        if (!active && options.onFiled) {
          loaded = false;
          options.onFiled(result);
          return;
        }
        await refresh();
        if (result && result.id) goTo(result.id);
      } catch (error) {
        if (fileState) {
          fileState.busy = false;
          fileState.status = error.message || "Could not file issue.";
          renderFileTray();
          var button = document.querySelector("[data-file-action=submit]");
          if (button) button.disabled = false;
        }
      }
    }

    function onGlobalClick(event) {
      if (event.target.closest("[data-issue-file]") || event.target.closest("#issue-new-button")) openFileDialog();
      if (event.target.closest("[data-action=leave-person]")) leavePerson();
      if (event.target.closest("[data-close]") && !event.target.closest("video")) closeCloser();
    }
    function deactivate() {
      active = false;
      requestGeneration++;
      loading = false;
      closeCloser();
      if (fileState) closeFileDialog();
      setActive(false);
    }
    function destroy() {
      destroyed = true;
      document.removeEventListener("keydown", onKeydown, true);
      document.removeEventListener("click", onGlobalClick, true);
      window.removeEventListener("resize", sizeMedia);
      closeFileDialog();
      closeCloser();
      deactivate();
    }

    document.addEventListener("click", onGlobalClick, true);
    document.addEventListener("keydown", onKeydown, true);
    window.addEventListener("resize", sizeMedia);
    if (hosted) showHostedUnavailable();
    return {
      render: function () { if (hosted) showHostedUnavailable(); else render(); },
      refresh: refresh,
      deactivate: deactivate,
      destroy: destroy
    };
  }

  root.IssueDashboard = { mount: mount };
})(typeof window !== "undefined" ? window : globalThis);
