/* The issue Inbox (round four, take A). DOM and network only: every decision it can
   hand off (queues, labels, origins, formatting, filing checks, the refresh plan)
   lives in issue-view-logic.js under node:test. */
(function (root) {
  "use strict";

  var BUBBLE = '<svg viewBox="0 0 16 16" aria-hidden="true"><path fill="currentColor" d="M2 2h12a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1H7l-3.5 3v-3H2a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z"/></svg>';

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
    var detailsStale = Object.create(null);
    var detailsLoading = Object.create(null);
    var detailEpochs = Object.create(null);
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
    var unavailable = false;
    var panel = null;
    var recorder = null;
    var recorderStream = null;
    var countLoading = false;
    var dragDepth = 0;
    var dropHint = null;
    var destroyed = false;

    // ---- actors and origins ----
    function actorText(actor) {
      if (typeof actor === "string") return actor;
      var normalized = root.normalizeActor ? root.normalizeActor(actor) : null;
      return normalized || (root.actorTooltip ? root.actorTooltip(actor) : String(actor || ""));
    }
    function actorChip(actor) {
      var full = actorText(actor);
      if (!full) return '<span class="issue-muted">unknown</span>';
      var kind = full.indexOf("human:") === 0 ? "human" : "agent";
      return '<span class="issue-actor issue-actor-' + kind + '" data-actor="' + esc(full) + '">' + esc(full) + "</span>";
    }
    function originHtml(actor, origin) {
      var label = logic.originLabel(actorText(actor), origin);
      return label ? ' <span class="issue-origin">· ' + esc(label) + "</span>" : "";
    }
    function actorHtml(actor, origin) { return actorChip(actor) + originHtml(actor, origin); }
    function displayTime(timestamp) {
      if (!timestamp) return "unknown time";
      var date = new Date(timestamp);
      if (isNaN(date.getTime())) return String(timestamp);
      return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(date);
    }
    function relativeTime(timestamp) { return logic.relativeTime(timestamp, Date.now()); }
    function mediaOf(issue) { return logic.liveMedia(issue); }
    function mediaUrl(path) { return root.apiUrl(options.basePath, path); }

    // ---- queues and the cursor ----
    function issueById(id, source) {
      var rows = source || (person ? personRows : issues);
      return rows.find(function (issue) { return issue.id === id; }) || details[id] || null;
    }
    function visibleRows() { return person ? personRows : issues; }
    // An issue reference (its id or short ID) as the short ID people read.
    function issueRef(ref) {
      var found = issues.find(function (item) { return item.id === ref || item.short_id === ref; }) ||
        personRows.find(function (item) { return item.id === ref || item.short_id === ref; });
      return found ? found.short_id || found.id : ref;
    }
    // The queue order: oldest first; on a person page, that person's latest activity first.
    function compareRows(a, b) {
      if (person) return logic.latestActorActivity(b, person.actor).localeCompare(logic.latestActorActivity(a, person.actor)) ||
        (b.seq || 0) - (a.seq || 0);
      return (a.seq || 0) - (b.seq || 0);
    }
    function rowsForQueue(key, sourceRows) {
      var source = sourceRows || visibleRows();
      if (person && key === "all") return logic.sortPersonIssues(source, person.actor);
      return logic.rowsForQueue(source, key, person ? person.actor : null).sort(compareRows);
    }
    // The selected issue as the board knows it now, in this queue or not.
    function knownIssue(id) {
      if (id == null) return null;
      return issues.find(function (issue) { return issue.id === id; }) ||
        personRows.find(function (issue) { return issue.id === id; }) || null;
    }
    function currentKey() { return (person ? "p:" + person.actor + ":" : "") + queue; }
    function current() { return issueById(cursors[currentKey()]); }
    function setCursor(issue) {
      if (issue) cursors[currentKey()] = issue.id;
      else delete cursors[currentKey()];
    }
    // Keep the cursor on the same issue. One that has left this queue stays selected (shown,
    // no row lit) unless the reader switched queue (move); then, or when it is gone from
    // the board, take whatever now sits at its old position.
    function settleCursor(rows, move) {
      var key = currentKey();
      var id = cursors[key];
      var index = rows.findIndex(function (issue) { return issue.id === id; });
      var off = index < 0 && !move ? knownIssue(id) : null;
      if (off) {
        cursors[key + ":index"] = logic.slotOf(rows, off, compareRows);
        return;
      }
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

    // ---- frame ----
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
          '<div class="issue-d-head" id="issue-d-head"><button type="button" class="issue-id-copy" id="issue-id-copy" title="Copy the ID (c), to tell an agent what to do with it" disabled></button>' +
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
        if (button) { button.blur(); switchQueue(button.getAttribute("data-q")); }
      });
      document.getElementById("issue-q-list").addEventListener("click", function (event) {
        var actor = event.target.closest("[data-actor]");
        if (actor) { enterPerson(actor.getAttribute("data-actor")); return; }
        var row = event.target.closest("[data-issue-id]");
        if (!row) return;
        setCursor(issueById(row.getAttribute("data-issue-id")));
        render();
      });
      document.getElementById("issue-id-copy").addEventListener("click", function () { this.blur(); copyId(); });
      document.getElementById("issue-d-head").addEventListener("click", function (event) {
        var actor = event.target.closest("[data-actor]");
        if (actor) enterPerson(actor.getAttribute("data-actor"));
      });
      document.getElementById("issue-d-body").addEventListener("click", onDetailClick);
      document.getElementById("issue-d-body").addEventListener("dblclick", function (event) {
        var figure = event.target.closest(".issue-media-item");
        if (figure && event.target.closest(".issue-media-frame")) openCloser(Number(figure.getAttribute("data-item")));
      });
    }
    function setActive(on) {
      document.body.classList.toggle("issue-view-active", !!on);
    }
    function showUnavailable() {
      setActive(false);
      mediaKey = null;
      app.innerHTML = '<section class="issue-unavailable"><h1>Issues</h1><p>Issues are not available on this board yet.</p></section>';
    }
    function markUnavailable() {
      unavailable = true;
      closeCloser();
      if (active) showUnavailable();
    }

    // ---- loading and refreshing ----
    async function loadIssues() {
      if (hosted || destroyed || unavailable) return;
      var wasLoaded = loaded;
      var previousIssues = issues;
      var previousPersonRows = personRows;
      var previousVisible = rowsForQueue(queue);
      var selected = current();
      var selectedId = selected ? selected.id : null;
      var selectedIndex = cursors[currentKey() + ":index"] || 0;
      loading = true;
      var generation = ++requestGeneration;
      try {
        var rows = await api("/api/issues");
        if (destroyed || generation !== requestGeneration) return;
        issues = Array.isArray(rows) ? rows : [];
        setNavCount(issues);
        if (person) await loadPersonIssues(person.actor, generation);
        if (destroyed || generation !== requestGeneration) return;
        loading = false;
        loaded = true;
        if (!active) return;
        if (!wasLoaded) { render(); return; }

        var changed = Object.create(null);
        logic.refreshDelta(previousIssues, issues, null).changedIds.forEach(function (id) { changed[id] = true; markDetailStale(id); });
        var personDelta = logic.refreshDelta(previousPersonRows, personRows, null);
        personDelta.changedIds.forEach(function (id) { changed[id] = true; markDetailStale(id); });
        renderQueueCounts();
        if (personDelta.listChanged) renderPersonHead();

        var nextVisible = rowsForQueue(queue);
        var selectedNow = knownIssue(selectedId);
        var plan = logic.planRefresh({
          previousRows: previousVisible,
          nextRows: nextVisible,
          selectedId: selectedId,
          selectedIndex: selectedIndex,
          selectedSlot: selectedNow ? logic.slotOf(nextVisible, selectedNow, compareRows) : null,
          selectedExists: !!selectedNow
        });
        if (plan.cursor.id == null) delete cursors[currentKey()];
        else cursors[currentKey()] = plan.cursor.id;
        cursors[currentKey() + ":index"] = plan.cursor.index;
        // The rows, or only the lit row: the highlight follows the cursor either way.
        if (plan.list || plan.cursor.id !== selectedId) renderQueue(nextVisible, { preserveScroll: true });
        updateQueueAges(nextVisible);
        var detail = plan.detail === "keep" && selectedId && changed[selectedId] ? "reload" : plan.detail;
        if (detail === "switch") renderDetail(current());
        else if (detail === "reload" && current()) {
          markDetailStale(current().id);
          ensureDetail(current());
        }
        syncCloser();
      } catch (error) {
        if (generation !== requestGeneration) return;
        loading = false;
        loaded = true;
        if (logic.isUnavailable(error)) markUnavailable();
        else if (!wasLoaded && active) showLoadError(error);
      }
    }
    async function loadPersonIssues(actor, parentGeneration) {
      if (hosted || destroyed) return;
      var rows = await api("/api/issues?by=" + encodeURIComponent(actor));
      if (destroyed || (parentGeneration != null && parentGeneration !== requestGeneration)) return;
      personRows = Array.isArray(rows) ? logic.sortPersonIssues(rows, actor) : [];
    }
    function setNavCount(rows) {
      var badge = document.getElementById("issue-nav-count");
      if (!badge) return;
      var open = rows.filter(function (issue) { return logic.queueOf(issue) === "open"; }).length;
      setTextIfChanged(badge, String(open));
      var title = open + (open === 1 ? " issue has" : " issues have") + " no story yet";
      if (badge.title !== title) badge.title = title;
    }
    // The nav count on every other view; the Inbox keeps it current itself.
    function refreshCount() {
      if (hosted || destroyed || unavailable || active || countLoading) return Promise.resolve();
      countLoading = true;
      return api("/api/issues").then(function (rows) {
        if (!destroyed && Array.isArray(rows)) setNavCount(rows);
      }, function (error) {
        if (logic.isUnavailable(error)) unavailable = true;
      }).then(function () { countLoading = false; });
    }
    function showLoadError(error) {
      var list = document.getElementById("issue-q-list");
      if (list) setHtmlIfChanged(list, '<div class="issue-q-empty">Could not load issues: ' + esc(error.message || error) + "</div>");
      var body = document.getElementById("issue-d-body");
      if (body) {
        mediaKey = null;
        setHtmlIfChanged(body, '<div class="issue-quiet">Refresh to try again.</div>');
      }
    }
    function refresh() {
      if (hosted || unavailable) { active = true; showUnavailable(); return Promise.resolve(); }
      active = true;
      if (!document.getElementById("issue-q-tabs")) { render(); return Promise.resolve(); }
      setActive(true);
      return loadIssues();
    }
    function render(renderOptions) {
      renderOptions = renderOptions || {};
      if (destroyed) return;
      active = true;
      if (hosted || unavailable) { showUnavailable(); return; }
      setActive(true);
      var tabs = document.getElementById("issue-q-tabs");
      var fresh = !tabs;
      if (fresh) {
        mountFrame();
        shownId = null;
        mediaKey = null;
      }
      if (!loading && (!loaded || fresh)) loadIssues();
      renderQueueCounts();
      var rows = rowsForQueue(queue);
      settleCursor(rows, !!renderOptions.settle);
      renderPersonHead();
      if (loading && !loaded) {
        setHtmlIfChanged(document.getElementById("issue-q-list"), '<div class="issue-q-empty">Loading issues…</div>');
      } else renderQueue(rows, { preserveScroll: !!renderOptions.preserveScroll });
      if (loaded) renderDetail(current());
      syncCloser();
    }
    function renderQueueCounts() {
      var tabs = document.getElementById("issue-q-tabs");
      if (!tabs) return;
      queues.forEach(function (item) {
        var count = person ? logic.rowsForQueue(personRows, item.key, person.actor).length :
          issues.filter(function (issue) { return logic.queueOf(issue) === item.key; }).length;
        setTextIfChanged(document.getElementById("issue-qc-" + item.key), String(count));
        var tab = tabs.querySelector('[data-q="' + item.key + '"]');
        var pressed = queue === item.key ? "true" : "false";
        if (tab && tab.getAttribute("aria-pressed") !== pressed) tab.setAttribute("aria-pressed", pressed);
      });
    }
    // Look closer follows the cursor while it has something to show, and steps aside when it does not.
    function syncCloser() {
      if (closer && (!current() || closer.issue !== current().id)) {
        if (current() && mediaOf(currentDetail()).length) openCloser(0);
        else closeCloser();
      }
    }
    function markDetailStale(id) {
      if (!id) return;
      detailsStale[id] = true;
      detailEpochs[id] = (detailEpochs[id] || 0) + 1;
    }
    function setHtmlIfChanged(element, html) {
      if (!element || element.__issueViewHtml === html) return;
      element.innerHTML = html;
      element.__issueViewHtml = html;
    }
    function setTextIfChanged(element, text) {
      if (element && element.textContent !== text) element.textContent = text;
    }

    // ---- the person page head ----
    function renderPersonHead() {
      var head = document.getElementById("issue-person-head");
      head.classList.toggle("on", !!person);
      if (!person) {
        setHtmlIfChanged(head, "<span>Click a name to see everything from that person.</span>");
        return;
      }
      var summary = logic.personSummary(personRows, person.actor);
      setHtmlIfChanged(head, '<div class="issue-person-line">' + actorChip(person.actor) +
        '<span class="issue-person-counts">' + summary.filed + " filed · " + summary.comments +
        (summary.comments === 1 ? " comment" : " comments") + '</span></div><div class="issue-person-line">' +
        '<span class="issue-person-machines">from ' + esc(summary.machines.join(", ") || "unknown") + "</span>" +
        '<button type="button" class="btn btn-sm" data-action="leave-person" title="Back to all issues (Esc)"><kbd>Esc</kbd>All</button></div>');
    }

    // ---- the queue ----
    function renderQueue(rows, renderOptions) {
      renderOptions = renderOptions || {};
      var list = document.getElementById("issue-q-list");
      var scrollTop = list.scrollTop;
      if (!rows.length) {
        setHtmlIfChanged(list, '<div class="issue-q-empty">' + esc(emptyMessage()) + "</div>");
      } else {
        if (list.__issueViewHtml !== undefined) {
          while (list.firstChild) list.removeChild(list.firstChild);
          delete list.__issueViewHtml;
        }
        var existing = Object.create(null);
        list.querySelectorAll(".issue-q-row[data-issue-id]").forEach(function (row) {
          existing["$" + row.getAttribute("data-issue-id")] = row;
        });
        var wanted = Object.create(null);
        var selected = cursors[currentKey()];
        rows.forEach(function (issue, index) {
          var key = "$" + issue.id;
          var markup = queueRowMarkup(issue, issue.id === selected);
          var row = existing[key];
          if (!row || row.__issueRowMarkup !== markup) {
            var template = document.createElement("template");
            template.innerHTML = markup;
            var replacement = template.content.firstElementChild;
            replacement.__issueRowMarkup = markup;
            if (row) row.replaceWith(replacement);
            row = replacement;
          }
          wanted[key] = true;
          var currentAtIndex = list.children[index];
          if (currentAtIndex !== row) list.insertBefore(row, currentAtIndex || null);
        });
        Object.keys(existing).forEach(function (key) {
          if (!wanted[key] && existing[key].parentNode === list) existing[key].remove();
        });
      }
      if (renderOptions.preserveScroll) list.scrollTop = scrollTop;
      else {
        var selectedRow = list.querySelector(".cursor");
        if (selectedRow) selectedRow.scrollIntoView({ block: "nearest" });
      }
    }
    function queueThumb(issue) {
      var media = mediaOf(issue);
      if (!media.length) return '<span class="issue-q-thumb none"></span>';
      var item = logic.rowMedia(media);
      var still = logic.stillOf(item);
      var photos = media.filter(function (m) { return m.kind !== "video"; }).length;
      var videos = media.length - photos;
      var label = [photos ? photos + (photos === 1 ? " photo" : " photos") : "", videos ? videos + (videos === 1 ? " video" : " videos") : ""]
        .filter(Boolean).join(" and ");
      return '<span class="issue-q-thumb" title="' + esc(label) + '">' +
        (still ? '<img loading="lazy" decoding="async" src="' + esc(mediaUrl(still)) + '" alt="">' : "") +
        (item.kind === "video" ? '<span class="issue-q-badge issue-q-video">▶ ' +
          esc(logic.formatDuration(typeof item.duration_ms === "number" ? item.duration_ms / 1000 : null)) + "</span>" : "") +
        (media.length > 1 ? '<span class="issue-q-badge issue-q-count-badge">' + media.length + "</span>" : "") + "</span>";
    }
    function queueRowMarkup(issue, selected) {
      var commentCount = Number(issue.comment_count || 0);
      return '<div class="issue-q-row' + (selected ? " cursor" : "") + '" data-issue-id="' + esc(issue.id) +
        '" role="option" aria-selected="' + (selected ? "true" : "false") + '"><div class="issue-q-main"><div class="issue-q-top">' +
        '<span class="issue-iid">' + esc(issue.short_id || issue.id) + '</span><span class="issue-q-tag">' + tagHtml(issue) + "</span>" +
        '<span class="issue-q-comments" title="' + commentCount + (commentCount === 1 ? " comment" : " comments") + '">' +
        (commentCount ? BUBBLE + commentCount : "") + '</span><span class="issue-q-age" title="' + esc(displayTime(issue.filed_at)) + '">' +
        esc(relativeTime(issue.filed_at)) + '</span></div><div class="issue-q-text" title="' + esc(issue.title || "") + '">' +
        esc(issue.title || "Untitled issue") + "</div></div>" + queueThumb(issue) + "</div>";
    }
    function tagHtml(issue) {
      var tag = logic.rowTag(issue, person ? person.actor : null);
      var why = tag.commented ? '<span class="issue-q-why">commented</span> · ' : "";
      if (tag.kind === "actor") {
        var full = actorText(tag.actor);
        return why + '<span class="issue-q-actor" data-actor="' + esc(full) + '">' + esc(full || "unknown") + "</span>";
      }
      if (tag.kind === "duplicate") return why + "duplicate of " + esc(issueRef(tag.of));
      if (tag.kind === "dismissed") return why + "dismissed";
      if (tag.kind === "state") return why + esc(tag.text);
      return why + '<span class="issue-tid">' + esc(tag.task) + "</span>" + (tag.more ? " +" + tag.more : "") + " " + esc(tag.status);
    }
    function updateQueueAges(rows) {
      var list = document.getElementById("issue-q-list");
      if (!list) return;
      list.querySelectorAll(".issue-q-row[data-issue-id]").forEach(function (row) {
        var issue = rows.find(function (item) { return item.id === row.getAttribute("data-issue-id"); });
        var age = row.querySelector(".issue-q-age");
        if (!issue || !age) return;
        setTextIfChanged(age, relativeTime(issue.filed_at));
      });
    }
    function emptyMessage() {
      if (person && queue === "all") return logic.EMPTY.person;
      return logic.EMPTY[queue] || "No issues are in this queue.";
    }

    // ---- the selected issue ----
    function currentDetail() {
      var issue = current();
      if (!issue) return null;
      return details[issue.id] || issue;
    }
    async function ensureDetail(issue) {
      if (!issue || hosted || detailsLoading[issue.id] || details[issue.id] && !detailsStale[issue.id]) return;
      var id = issue.id;
      var epoch = detailEpochs[id] || 0;
      var retry = false;
      detailsLoading[id] = true;
      try {
        var detail = await api("/api/issues/" + encodeURIComponent(id));
        if (destroyed) return;
        if (epoch !== (detailEpochs[id] || 0)) {
          retry = true;
        } else {
          details[id] = detail;
          delete detailsStale[id];
          if (active && current() && current().id === id) renderDetail(current());
        }
      } catch (error) {
        if (logic.isUnavailable(error)) { markUnavailable(); return; }
        var body = document.getElementById("issue-d-body");
        if (body && !details[id] && current() && current().id === id) {
          mediaKey = null;
          setHtmlIfChanged(body, '<div class="issue-quiet">Could not load issue: ' + esc(error.message || error) + "</div>");
        }
      } finally {
        delete detailsLoading[id];
      }
      if (retry && !destroyed) ensureDetail(issue);
    }
    function stateChip(state) {
      return '<span class="issue-state issue-state-' + esc(state) + '">' + esc(state) + "</span>";
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
        '<div class="issue-comments" id="issue-comments"></div>' +
        '<div class="issue-compose" id="issue-compose"><textarea id="issue-comment-box" rows="1" placeholder="Comment, or ask an agent: @claude-opus-impl ..."></textarea>' +
        '<button type="button" class="btn btn-sm" id="issue-comment-post" title="Post (⌘ Enter)">Post</button></div><div id="issue-history"></div>';
    }
    function wireCompose() {
      var box = document.getElementById("issue-comment-box");
      var wrap = document.getElementById("issue-compose");
      var post = document.getElementById("issue-comment-post");
      box.addEventListener("focus", function () { wrap.classList.add("active"); });
      box.addEventListener("blur", function () { if (!box.value.trim()) wrap.classList.remove("active"); });
      box.addEventListener("keydown", function (event) {
        if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); postComment(); }
        else if (event.key === "Escape") { event.preventDefault(); box.blur(); }
      });
      post.addEventListener("mousedown", function (event) { event.preventDefault(); }); // keep focus in the box
      post.addEventListener("click", postComment);
    }
    function renderDetail(issue) {
      var body = document.getElementById("issue-d-body");
      if (!body) return;
      var id = issue ? issue.id : "none:" + queue;
      var changed = id !== shownId;
      shownId = id;
      var copy = document.getElementById("issue-id-copy");
      setTextIfChanged(copy, issue ? issue.short_id || issue.id : "");
      copy.disabled = !issue;
      var detail = issue ? details[issue.id] || issue : null;
      setHtmlIfChanged(document.getElementById("issue-d-state"), issue ? stateChip(logic.stateOf(detail)) : "");
      setHtmlIfChanged(document.getElementById("issue-d-facts"), issue ? factsHtml(detail) : "");
      var status = document.getElementById("issue-copy-status");
      if (changed && !status.classList.contains("done")) setTextIfChanged(status, issue ? "copy" : "");
      if (!issue) {
        mediaKey = null;
        setHtmlIfChanged(body, person && queue === "all" ? '<div class="issue-quiet">' + esc(logic.EMPTY.person) + "</div>" :
          queue === "open" ? '<div class="issue-quiet"><h2>' + esc(logic.EMPTY.open) + "</h2>" + esc(logic.OPEN_DETAIL) + "</div>" :
          '<div class="issue-quiet">' + esc(emptyMessage()) + "</div>");
        return;
      }
      if (!details[issue.id]) {
        mediaKey = null;
        setHtmlIfChanged(body, '<div class="issue-quiet">Loading issue…</div>');
        ensureDetail(issue);
        return;
      }
      if (!document.getElementById("issue-d-title")) {
        mediaKey = null;
        setHtmlIfChanged(body, detailShell());
        wireCompose();
      }
      if (detailsStale[issue.id]) ensureDetail(issue);
      var scrollTop = changed ? 0 : body.scrollTop;
      setTextIfChanged(document.getElementById("issue-d-title"), detail.title || "Untitled issue");
      setTextIfChanged(document.getElementById("issue-d-desc"), detail.description || "");
      setHtmlIfChanged(document.getElementById("issue-comments"), commentsHtml(detail));
      if (changed) {
        document.getElementById("issue-comment-box").value = "";
        document.getElementById("issue-compose").classList.remove("active");
      }
      renderMedia(detail);
      setHtmlIfChanged(document.getElementById("issue-outcome"), outcomeHtml(detail));
      renderHistory(detail);
      if (body.scrollTop !== scrollTop) body.scrollTop = scrollTop;
    }
    // What came of it: its stories with their live status, or why it was closed.
    function outcomeHtml(issue) {
      if (issue.closure) {
        var closure = issue.closure;
        if (closure.kind === "duplicate") {
          var duplicate = closure.duplicate_of || "unknown";
          var target = issues.find(function (item) { return item.short_id === duplicate || item.id === duplicate; });
          var line = target ? String(target.title || "").split("\n")[0] : "";
          if (line.length > 100) line = line.slice(0, 97) + "...";
          return '<div class="issue-closure"><b>Duplicate of <a href="#" data-goto="' + esc(target ? target.id : duplicate) + '">' + esc(issueRef(duplicate)) +
            "</a>.</b> " + esc(line) + "</div>";
        }
        return '<div class="issue-closure"><b>Dismissed.</b> ' + esc(closure.reason || "") + "</div>";
      }
      return (issue.tasks || []).map(function (task) {
        var title = task.title || (task.erased ? "Erased task" : "Task unavailable");
        var statusKey = String(task.status || "unknown");
        return '<div class="issue-story' + (statusKey === "cancelled" || task.erased ? " dead" : "") + '"><span class="issue-tid">' +
          esc(task.short_id || task.id) + '</span><span class="issue-tstatus issue-tstatus-' + esc(statusKey) + '">' + esc(statusKey.replace(/_/g, " ")) +
          '</span><span class="issue-story-title" title="' + esc(title) + '">' + esc(title) + "</span></div>";
      }).join("");
    }
    // @mentions stand out, and story and issue IDs read as IDs.
    function commentBody(text) {
      return esc(text).replace(/(^|\s)(@[\w:.-]+)/g, '$1<span class="mention">$2</span>')
        .replace(/\b[A-Z][A-Z0-9]*-I\d+\b/g, function (value) { return '<span class="issue-iid">' + value + "</span>"; })
        .replace(/\b[A-Z][A-Z0-9]*-\d+\b/g, function (value) { return '<span class="issue-tid">' + value + "</span>"; });
    }
    function commentsHtml(issue) {
      var comments = logic.flattenComments(issue.comments || []);
      if (!comments.length) return "";
      return '<div class="issue-comments-head">Comments (' + comments.length + ")</div>" + comments.map(function (comment) {
        var author = logic.commentActor(comment) || "unknown";
        var time = logic.commentTime(comment);
        var kind = actorText(author).indexOf("human:") === 0 ? "human" : "agent";
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
        markDetailStale(issue.id);
        ensureDetail(issue);
        refresh();
      }).catch(function (error) { options.showToast(error.message || String(error), "error"); });
    }
    function mediaCountText(photos, videos) {
      var parts = [];
      if (photos) parts.push(photos + (photos === 1 ? " photo" : " photos"));
      if (videos) parts.push(videos + (videos === 1 ? " video" : " videos"));
      return parts.join(" and ");
    }
    function eventHistoryLine(event) {
      var data = event.data || {};
      var what;
      switch (event.type) {
        case "issue_filed": {
          var filedMedia = Array.isArray(data.media) ? data.media : [];
          var videos = filedMedia.filter(function (item) { return item && item.kind === "video"; }).length;
          var count = mediaCountText(filedMedia.length - videos, videos);
          what = "filed" + (count ? " with " + esc(count) : "");
          break;
        }
        case "issue_linked": what = (data.promoted ? "made story " : "linked to ") + '<span class="issue-tid">' + esc(data.task_id || "task") + "</span>"; break;
        case "issue_unlinked": what = 'unlinked from <span class="issue-tid">' + esc(data.task_id || "task") + "</span>"; break;
        case "issue_dismissed": what = "dismissed: " + esc(data.reason || ""); break;
        case "issue_marked_duplicate": what = 'marked a duplicate of <span class="issue-iid">' + esc(issueRef(data.duplicate_of || "issue")) + "</span>"; break;
        case "issue_reopened": what = "reopened"; break;
        case "issue_media_added": what = "added a " + esc((data.media && data.media.kind) || data.kind || "file"); break;
        case "issue_comment_added": what = "commented"; break;
        case "issue_media_removed": what = "removed a " + esc(data.kind || "file") + (data.reason ? ": " + esc(data.reason) : ""); break;
        default: what = esc(event.type || "changed");
      }
      var actor = event.actor || event.by || "unknown";
      var at = event.ts || event.at;
      return '<div><span class="issue-history-at" title="' + esc(displayTime(at)) + '">' + esc(relativeTime(at)) +
        '</span><span class="issue-history-what">' + actorHtml(actor, event.origin || data.origin) + " " + what + "</span></div>";
    }
    function renderHistory(issue) {
      var history = Array.isArray(issue.events) ? issue.events : [];
      var box = document.getElementById("issue-history");
      if (!box) return;
      setHtmlIfChanged(box, '<button type="button" class="issue-history-toggle" aria-expanded="' + historyOpen + '"><span class="caret">' +
        (historyOpen ? "▾" : "▸") + "</span>History (" + history.length + ")</button>" +
        (historyOpen ? '<div class="issue-history">' + history.map(eventHistoryLine).join("") + "</div>" : ""));
    }

    // ---- media inline ----
    function frameSeconds(frame) { return Number(frame.t_ms || 0) / 1000; }
    function renderMedia(issue) {
      var box = document.getElementById("issue-d-media");
      if (!box) return;
      var media = mediaOf(issue);
      var key = logic.mediaKey(issue.id, media);
      if (key === mediaKey) return;
      mediaKey = key;
      focusMedia = 0;
      setHtmlIfChanged(box, media.map(function (item, index) {
        var src = mediaUrl(item.url);
        var name = item.original_name || item.name || "attached media";
        var poster = item.kind === "video" ? logic.stillOf(item) : null;
        var content = item.kind === "video" ? '<video src="' + esc(src) + '"' + (poster ? ' poster="' + esc(mediaUrl(poster)) + '"' : "") +
          ' muted playsinline preload="metadata" title="' + esc(name) + '"></video>' +
          '<button type="button" class="issue-media-play" data-play="' + index + '" title="Play (muted)">▶</button>' :
          '<img src="' + esc(src) + '" alt="photo attached to ' + esc(issue.short_id || issue.id) + '" title="' + esc(name) + '">';
        var frames = item.kind === "video" ? (item.frames || []).filter(function (frame) { return frame.url; }) : [];
        var agentFrames = item.kind === "video" ? '<div class="issue-af">' + (frames.length ?
          '<div class="issue-af-label">What an agent sees: these frames, taken when it was filed. Click one to see that moment.</div><div class="issue-af-row">' +
          frames.map(function (frame, frameIndex) {
            var at = logic.formatDuration(frameSeconds(frame), true);
            return '<button type="button" class="issue-af-frame" data-frame="' + frameIndex + '" data-item="' + index + '"><img src="' +
              esc(mediaUrl(frame.url)) + '" alt="frame at ' + esc(at) + '" loading="lazy"><span>' + esc(at) + "</span></button>";
          }).join("") + "</div>" :
          '<div class="issue-af-none">Frames are made when this is filed.</div>') + "</div>" : "";
        return '<figure class="issue-media-item" data-item="' + index + '"><div class="issue-media-frame ' + (item.kind === "video" ? "video" : "photo") +
          '" data-w="' + esc(item.width || 1280) + '" data-h="' + esc(item.height || 800) + '">' + content + "</div>" + agentFrames + "</figure>";
      }).join(""));
      box.querySelectorAll("video").forEach(function (video) {
        var figure = video.closest(".issue-media-item");
        var item = media[Number(figure.getAttribute("data-item"))];
        var frames = (item.frames || []).filter(function (frame) { return frame.url; });
        video.addEventListener("timeupdate", function () {
          var currentFrame = -1;
          frames.forEach(function (frame, index) { if (frameSeconds(frame) <= video.currentTime + 0.05) currentFrame = index; });
          figure.querySelectorAll(".issue-af-frame").forEach(function (frame, index) { frame.classList.toggle("on", index === currentFrame); });
        });
      });
      sizeMedia();
    }
    // Each item is shown at its own pixel size, shrunk only to fit the pane and 72% of the window height.
    function sizeMedia() {
      var box = document.getElementById("issue-d-media");
      if (!box) return;
      var maxWidth = box.clientWidth - 2;
      var maxHeight = Math.round(window.innerHeight * 0.72);
      box.querySelectorAll(".issue-media-frame").forEach(function (frame) {
        var width = Number(frame.getAttribute("data-w")) || 1280;
        var height = Number(frame.getAttribute("data-h")) || 800;
        var scale = Math.max(0, Math.min(1, maxWidth / width, maxHeight / height));
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
      var frames = media ? (media.frames || []).filter(function (frame) { return frame.url; }) : [];
      if (!video || !frames[frameIndex]) return;
      video.pause();
      video.controls = true;
      figure.querySelector(".issue-media-frame").classList.add("playing");
      video.currentTime = frameSeconds(frames[frameIndex]);
      figure.querySelectorAll(".issue-af-frame").forEach(function (frame, index) { frame.classList.toggle("on", index === frameIndex); });
      focusMedia = itemIndex;
    }

    // ---- look closer ----
    function openCloser(index) {
      var issue = currentDetail();
      var media = mediaOf(issue);
      if (!media.length) { closeCloser(); return; }
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
      var src = mediaUrl(item.url);
      var poster = item.kind === "video" ? logic.stillOf(item) : null;
      overlay.innerHTML = '<div class="issue-closer-bar"><span class="issue-iid">' + esc(issue.short_id || issue.id) +
        '</span><span class="issue-closer-title">' + esc(issue.title || "Issue") + '</span><span class="issue-closer-pos">' +
        (media.length > 1 ? (index + 1) + " of " + media.length : "") + '</span><span class="issue-closer-keys">' +
        (media.length > 1 ? "<kbd>←</kbd> <kbd>→</kbd> item &nbsp; " : "") + "<kbd>j</kbd> <kbd>k</kbd> issue &nbsp; <kbd>f</kbd> or <kbd>Esc</kbd> closes</span></div>" +
        '<div class="issue-closer-media" data-close>' + (item.kind === "video" ? '<video src="' + esc(src) + '"' +
          (poster ? ' poster="' + esc(mediaUrl(poster)) + '"' : "") + ' muted playsinline controls></video>' :
          '<img src="' + esc(src) + '" alt="">') + "</div>";
      var video = overlay.querySelector("video");
      if (video) {
        video.currentTime = start;
        if (wasPlaying) { var play = video.play(); if (play && play.catch) play.catch(function () {}); }
      }
      closer = { index: index, issue: current().id };
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
      if (play) { play.blur(); playInline(Number(play.getAttribute("data-play"))); return; }
      var frame = event.target.closest("[data-frame]");
      if (frame) { frame.blur(); seekFrame(Number(frame.getAttribute("data-item")), Number(frame.getAttribute("data-frame"))); return; }
      var toggle = event.target.closest(".issue-history-toggle");
      if (toggle) { toggle.blur(); historyOpen = !historyOpen; renderHistory(currentDetail()); return; }
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
    }

    // ---- keyboard: moving and looking only; decisions are made by telling an agent ----
    // The dashboard's own drawers and dialogs take the keyboard while they are open.
    function sharedPanelOpen() {
      if (document.querySelector("#settings-panel.open, #filter-panel.open, #detail-panel.open, #web-detail-pane.open")) return true;
      var modal = document.getElementById("create-task-modal");
      return !!(modal && modal.style.display && modal.style.display !== "none");
    }
    function onKeydown(event) {
      if (destroyed) return;
      if (panel) {
        if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeFileDialog(); }
        else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); event.stopPropagation(); panel.go(); }
        return;
      }
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (sharedPanelOpen()) return;
      var tag = (event.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select" || event.target.isContentEditable) return;
      var key = event.key;
      if (key === "i") {
        if (hosted) return;
        event.preventDefault();
        openFileDialog();
        return;
      }
      if (!active || unavailable || hosted || !document.body.classList.contains("issue-view-active")) return;
      var handled = true;
      if (key === "j" || key === "ArrowDown") move(1);
      else if (key === "k" || key === "ArrowUp") move(-1);
      else if (key === "c") copyId();
      else if (key === "f") { if (closer) closeCloser(); else if (mediaOf(currentDetail()).length) openCloser(focusMedia); }
      else if (key === "Escape") { if (closer) closeCloser(); else if (person) leavePerson(); else handled = false; }
      else if (closer && key === "ArrowRight") openCloser(closer.index + 1);
      else if (closer && key === "ArrowLeft") openCloser(closer.index - 1);
      else if (key === " ") {
        var video = closer ? document.querySelector("#issue-closer video") : document.querySelector("#issue-d-media video");
        if (!video) handled = !!closer;
        else if (video.paused) {
          if (!closer) playInline(Number(video.closest(".issue-media-item").getAttribute("data-item")));
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
      var off = index < 0 ? knownIssue(cursors[key]) : null;
      var slot = off ? logic.slotOf(rows, off, compareRows) : cursors[key + ":index"] || 0;
      index = logic.stepIndex(rows.length, index, slot, delta);
      cursors[key] = rows[index].id;
      cursors[key + ":index"] = index;
      render();
    }
    // In the person view a tab narrows that person's list; pressing it again shows all of it.
    function switchQueue(key) {
      if (person) queue = queue === key ? "all" : key;
      else if (queue === key) return;
      else queue = key;
      render({ settle: true });
    }
    function goTo(id) {
      var issue = issueById(id, issues);
      if (!issue) return;
      if (person && personRows.some(function (item) { return item.id === id; })) {
        if (queue !== "all" && logic.queueOf(issue) !== queue) queue = "all";
        cursors[currentKey()] = id;
        render();
        return;
      }
      person = null;
      personRows = [];
      queue = logic.queueOf(issue);
      cursors[currentKey()] = issue.id;
      render();
    }
    function enterPerson(actor) {
      if (!actor || person && person.actor === actor) return;
      var keepId = cursors[currentKey()];
      if (!person) personBack = { queue: queue, id: keepId };
      person = { actor: actor };
      queue = "all";
      personRows = [];
      var list = document.getElementById("issue-q-list");
      if (list) list.scrollTop = 0;
      render();
      var generation = ++requestGeneration;
      loadPersonIssues(actor, generation).then(function () {
        if (person && person.actor === actor && generation === requestGeneration) {
          var rows = rowsForQueue(queue);
          if (keepId && rows.some(function (item) { return item.id === keepId; })) cursors[currentKey()] = keepId;
          else if (rows[0]) cursors[currentKey()] = rows[0].id;
          cursors[currentKey() + ":index"] = 0;
          render();
        }
      }).catch(function (error) {
        if (logic.isUnavailable(error)) markUnavailable();
        else showLoadError(error);
      });
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
      var text = issue.short_id || issue.id;
      var status = document.getElementById("issue-copy-status");
      function show(ok) {
        status.textContent = ok ? "Copied" : "Not copied";
        status.classList.toggle("done", ok);
        clearTimeout(copyTimer);
        copyTimer = setTimeout(function () { status.textContent = current() ? "copy" : ""; status.classList.remove("done"); }, 1600);
      }
      function fallback() {
        var area = document.createElement("textarea");
        area.value = text;
        area.setAttribute("readonly", ""); area.style.position = "fixed"; area.style.opacity = "0";
        document.body.appendChild(area); area.select();
        var copied = false;
        try { copied = document.execCommand("copy"); } catch (_error) { copied = false; }
        area.remove();
        return copied;
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { show(true); }, function () { show(fallback()); });
      } else show(fallback());
    }

    // ---- the quick-file panel ----
    // One field is required: the title. Photos and video are first-class but optional.
    function readMedia(file) {
      return new Promise(function (resolve) {
        var kind = logic.kindOf(file);
        var item = { kind: kind, bytes: file.size || 0, duration: null, src: null, poster: null, broken: false };
        try { item.src = URL.createObjectURL(file); } catch (_error) { item.broken = true; resolve(item); return; }
        var settled = false;
        var timer = setTimeout(function () { finish(); }, 6000);
        function finish() { if (!settled) { settled = true; clearTimeout(timer); resolve(item); } }
        if (kind === "photo") {
          var image = new Image();
          image.onload = finish;
          image.onerror = function () { item.broken = true; finish(); };
          image.src = item.src;
          return;
        }
        var video = document.createElement("video");
        video.muted = true; video.preload = "auto"; video.playsInline = true;
        video.onloadedmetadata = function () {
          var seconds = isFinite(video.duration) ? video.duration : null;
          item.duration = seconds ? Math.round(seconds * 10) / 10 : null;
          try { video.currentTime = seconds ? Math.min(1, seconds / 4) : 0.2; } catch (_error) { finish(); }
        };
        video.onseeked = function () {
          try {
            var width = video.videoWidth || 640;
            var height = video.videoHeight || 400;
            var scale = Math.min(1, 640 / width);
            var canvas = document.createElement("canvas");
            canvas.width = Math.round(width * scale); canvas.height = Math.round(height * scale);
            canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
            item.poster = canvas.toDataURL("image/jpeg", 0.8);
          } catch (_error) { /* no poster: the tile shows the kind instead */ }
          finish();
        };
        video.onerror = function () { item.broken = true; finish(); };
        video.src = item.src;
      });
    }
    function clipFiles(data) {
      var out = [];
      if (!data) return out;
      if (data.items && data.items.length) {
        Array.prototype.forEach.call(data.items, function (entry) {
          if (entry.kind === "file") { var file = entry.getAsFile(); if (file) out.push(file); }
        });
      }
      if (!out.length && data.files) Array.prototype.forEach.call(data.files, function (file) { if (file) out.push(file); });
      return out;
    }
    function hasType(transfer, type) { return !!(transfer && transfer.types && Array.prototype.indexOf.call(transfer.types, type) >= 0); }
    function limits() { return options.mediaLimits ? options.mediaLimits() : logic.DEFAULT_LIMITS; }

    function openFileDialog(prefill) {
      prefill = prefill || {};
      if (hosted || destroyed) return;
      if (panel) {
        if (prefill.files) panel.addFiles(prefill.files);
        return;
      }
      var overlay = document.createElement("div");
      overlay.className = "issue-file-overlay";
      overlay.id = "issue-file-dialog";
      overlay.innerHTML = '<div class="issue-dialog issue-dialog-file" role="dialog" tabindex="-1" aria-label="File an issue">' +
        '<div class="issue-dialog-head"><span>File an issue</span><button type="button" class="btn btn-sm" data-file-action="close" title="Close (Esc)">Esc</button></div>' +
        '<div class="issue-dialog-body">' +
          '<label for="issue-fi-text">Title</label>' +
          '<input type="text" id="issue-fi-text" autocomplete="off" placeholder="Footer overlaps the Complete button at 400px">' +
          '<label for="issue-fi-desc">Description <span class="issue-muted">(optional)</span></label>' +
          '<textarea id="issue-fi-desc" placeholder="Steps, what you expected, what happened"></textarea>' +
          '<div class="issue-fi-err" id="issue-fi-err"></div>' +
          '<div class="issue-fi-media-head"><label>Photos and video <span class="issue-muted">(optional)</span></label><span class="issue-fi-total" id="issue-fi-total"></span></div>' +
          '<div class="issue-tray" id="issue-fi-tray"></div>' +
          '<div class="issue-fi-media-actions">' +
            '<button type="button" class="btn issue-fi-choose" data-file-action="choose">Choose a file</button>' +
            '<button type="button" class="btn issue-fi-record" data-file-action="record">Record video</button>' +
            '<input type="file" id="issue-fi-input" accept="image/*,video/*" multiple hidden>' +
          "</div>" +
        "</div>" +
        '<div class="issue-dialog-foot"><span class="issue-hint"><kbd>⌘</kbd> <kbd>V</kbd> pastes a screenshot. <kbd>⌘</kbd> <kbd>Enter</kbd> files it. No story is made.</span>' +
          '<button type="button" class="btn btn-primary issue-fi-go" data-file-action="submit">File issue</button></div>' +
        "</div>";
      document.body.appendChild(overlay);
      var text = document.getElementById("issue-fi-text");
      var descriptionField = document.getElementById("issue-fi-desc");
      var err = document.getElementById("issue-fi-err");
      var trayEl = document.getElementById("issue-fi-tray");
      var input = document.getElementById("issue-fi-input");
      var tray = []; // { state: "reading" | "ok" | "refused", file, item, reason, name, bytes }
      var busy = false;

      function accepted() { return tray.filter(function (entry) { return entry.state === "ok"; }); }
      function acceptedBytes() { return accepted().reduce(function (total, entry) { return total + entry.bytes; }, 0); }
      function drawTray() {
        setTextIfChanged(document.getElementById("issue-fi-total"), logic.trayTotal(acceptedBytes(), limits()));
        if (!tray.length) {
          trayEl.innerHTML = '<div class="issue-tray-empty"><b>Paste, drop or choose a photo or video</b><span>A screenshot on the clipboard goes in with <kbd>⌘</kbd> <kbd>V</kbd>.</span></div>';
          return;
        }
        trayEl.innerHTML = tray.map(function (entry, index) {
          var item = entry.item || {};
          var thumb = entry.state === "refused" ? '<div class="issue-tile-thumb refused">Not attached</div>' :
            entry.state === "reading" ? '<div class="issue-tile-thumb">Reading...</div>' :
            item.kind === "photo" && item.src ? '<div class="issue-tile-thumb"><img src="' + esc(item.src) + '" alt=""></div>' :
            item.poster ? '<div class="issue-tile-thumb"><img src="' + esc(item.poster) + '" alt=""><span class="play">▶</span></div>' :
            '<div class="issue-tile-thumb">' + (item.kind === "video" ? "video" : "photo") + "</div>";
          var meta = entry.state === "refused" ? '<span class="issue-tile-why">' + esc(entry.reason) + "</span>" :
            '<span class="issue-tile-kind">' + (item.kind === "video" ? "video " + esc(logic.formatDuration(item.duration)) : "photo") +
            '</span><span class="issue-tile-size">' + esc(logic.formatBytes(entry.bytes)) + "</span>";
          return '<div class="issue-tile' + (entry.state === "refused" ? " issue-tile-refused" : "") + '" title="' + esc(entry.name) + '">' + thumb +
            '<div class="issue-tile-meta">' + meta + '</div><button type="button" class="issue-tile-x" data-file-action="remove" data-index="' + index + '" title="Remove">×</button></div>';
        }).join("");
      }
      function addFiles(files) {
        err.textContent = "";
        Array.prototype.forEach.call(files || [], function (file) {
          if (!file) return;
          var kind = logic.kindOf(file);
          var entry = { state: "reading", file: file, name: file.name || "file", bytes: file.size || 0, item: { kind: kind } };
          var why = logic.mediaProblem(acceptedBytes() + tray.filter(function (t) { return t.state === "reading"; })
            .reduce(function (total, t) { return total + t.bytes; }, 0), { kind: kind, bytes: entry.bytes }, limits());
          if (why) { entry.state = "refused"; entry.reason = why; tray.push(entry); return; }
          tray.push(entry);
          readMedia(file).then(function (item) {
            entry.item = item;
            if (item.broken) { entry.state = "refused"; entry.reason = "This file could not be read as a " + (item.kind === "video" ? "video" : "photo") + "."; }
            else entry.state = "ok";
            if (panel && panel.tray === tray) drawTray();
          });
        });
        drawTray();
      }
      function removeAt(index) {
        var entry = tray[index];
        if (entry && entry.item && entry.item.src) { try { URL.revokeObjectURL(entry.item.src); } catch (_error) { /* ignore */ } }
        tray.splice(index, 1);
        drawTray();
      }
      async function go() {
        if (busy) return;
        err.textContent = "";
        var ok = accepted();
        var problem = logic.filingProblem(text.value, {
          reading: tray.filter(function (entry) { return entry.state === "reading"; }).length,
          accepted: ok.length,
          firstKind: ok.length ? ok[0].item.kind : null
        });
        if (problem) { err.textContent = problem; text.focus(); return; }
        busy = true;
        var button = overlay.querySelector("[data-file-action=submit]");
        button.disabled = true;
        try {
          var title = text.value.trim();
          var description = descriptionField.value;
          var media = [];
          for (var i = 0; i < ok.length; i++) media.push(await mediaFromFile(ok[i].file));
          if (panel !== state) return;
          var result = await apiPost("/api/issues", { title: title, description: description, media: media });
          closeFileDialog();
          if (options.showToast) options.showToast("Filed " + ((result && (result.short_id || result.id)) || "the issue") + ": " + title);
          if (active) refresh();
          else { loaded = false; refreshCount(); }
        } catch (error) {
          busy = false;
          button.disabled = false;
          err.textContent = error.message || "Could not file the issue.";
          text.focus();
        }
      }
      var state = { overlay: overlay, addFiles: addFiles, go: go, tray: tray };
      panel = state;
      overlay.addEventListener("mousedown", function (event) { if (event.target === overlay) closeFileDialog(); });
      overlay.addEventListener("click", function (event) {
        var action = event.target.closest("[data-file-action]");
        if (!action) return;
        var name = action.getAttribute("data-file-action");
        if (name === "close") closeFileDialog();
        else if (name === "choose") input.click();
        else if (name === "record") toggleRecording(action);
        else if (name === "submit") go();
        else if (name === "remove") removeAt(Number(action.getAttribute("data-index")));
      });
      input.addEventListener("change", function () { addFiles(input.files); input.value = ""; text.focus(); });
      text.addEventListener("keydown", function (event) {
        if (event.key === "Enter" && !event.metaKey && !event.ctrlKey) { event.preventDefault(); descriptionField.focus(); }
      });
      drawTray();
      if (prefill.files) addFiles(prefill.files);
      text.focus();
    }
    function closeFileDialog() {
      if (recorder && recorder.state !== "inactive") { recorder.onstop = null; recorder.stop(); }
      if (recorderStream) recorderStream.getTracks().forEach(function (track) { track.stop(); });
      recorder = null;
      recorderStream = null;
      if (!panel) return;
      var closing = panel;
      panel = null;
      closing.tray.forEach(function (entry) {
        if (entry.item && entry.item.src) { try { URL.revokeObjectURL(entry.item.src); } catch (_error) { /* ignore */ } }
      });
      closing.overlay.remove();
    }
    async function toggleRecording(button) {
      if (!panel) return;
      var err = document.getElementById("issue-fi-err");
      if (recorder && recorder.state !== "inactive") { recorder.stop(); return; }
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || typeof MediaRecorder === "undefined") {
        err.textContent = "Video recording is not available in this browser.";
        return;
      }
      var owner = panel;
      try {
        recorderStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
        if (panel !== owner) {
          recorderStream.getTracks().forEach(function (track) { track.stop(); });
          recorderStream = null;
          return;
        }
        var mime = MediaRecorder.isTypeSupported("video/mp4") ? "video/mp4" : "video/webm";
        recorder = new MediaRecorder(recorderStream, { mimeType: mime });
        var chunks = [];
        recorder.ondataavailable = function (event) { if (event.data && event.data.size) chunks.push(event.data); };
        recorder.onstop = function () {
          if (recorderStream) recorderStream.getTracks().forEach(function (track) { track.stop(); });
          recorder = null;
          recorderStream = null;
          button.textContent = "Record video";
          if (chunks.length && panel === owner) {
            owner.addFiles([new File(chunks, "Recording." + (mime.indexOf("mp4") >= 0 ? "mp4" : "webm"), { type: mime })]);
          }
        };
        recorder.start();
        button.textContent = "Stop recording";
      } catch (error) {
        err.textContent = error.message || "Could not start recording.";
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
    function waitFor(target, name, timeoutMs) {
      return new Promise(function (resolve, reject) {
        var timer = null;
        function cleanup() {
          if (timer) clearTimeout(timer);
          target.removeEventListener(name, ready);
          target.removeEventListener("error", failed);
        }
        function ready(event) { cleanup(); resolve(event); }
        function failed() { cleanup(); reject(new Error("Could not read the video.")); }
        target.addEventListener(name, ready, { once: true });
        target.addEventListener("error", failed, { once: true });
        if (timeoutMs) timer = setTimeout(function () { cleanup(); resolve(null); }, timeoutMs);
      });
    }
    // A recorded WebM can report an Infinity duration until it is seeked: seeking
    // far past the end makes the browser work the real duration out.
    async function knownDuration(video) {
      if (isFinite(video.duration) && video.duration > 0) return video.duration;
      var changed = waitFor(video, "durationchange", 3000);
      try { video.currentTime = 1e101; } catch (_error) { return null; }
      await changed;
      var seconds = isFinite(video.duration) && video.duration > 0 ? video.duration : null;
      var back = waitFor(video, "seeked", 3000);
      video.currentTime = 0;
      await back;
      return seconds;
    }
    async function videoFrames(video, durationMsValue) {
      var frames = [];
      var sampleTimes = logic.frameTimes(durationMsValue);
      for (var i = 0; i < sampleTimes.length; i++) {
        var tMs = sampleTimes[i];
        if (frames.length && frames[frames.length - 1].t_ms === tMs) continue;
        if (Math.abs(video.currentTime - tMs / 1000) > 0.001) {
          var seeked = waitFor(video, "seeked", 5000);
          video.currentTime = tMs / 1000;
          await seeked;
        }
        var scale = Math.min(1, 1568 / Math.max(video.videoWidth || 1, video.videoHeight || 1));
        var canvas = document.createElement("canvas");
        canvas.width = Math.max(1, Math.round((video.videoWidth || 1) * scale));
        canvas.height = Math.max(1, Math.round((video.videoHeight || 1) * scale));
        canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
        var frameBlob = await new Promise(function (resolve) { canvas.toBlob(resolve, "image/jpeg", 0.82); });
        if (frameBlob) frames.push({ t_ms: tMs, payload: await payloadFromBlob(frameBlob, "frame.jpg") });
      }
      return frames;
    }
    async function mediaFromFile(file) {
      var payload = await payloadFromBlob(file, file.name || "attachment");
      if (logic.kindOf(file) !== "video") return { payload: payload };
      var objectUrl = URL.createObjectURL(file);
      var video = document.createElement("video");
      video.preload = "auto"; video.muted = true; video.playsInline = true;
      try {
        var metadata = waitFor(video, "loadedmetadata");
        var firstFrame = waitFor(video, "loadeddata", 5000);
        video.src = objectUrl;
        await metadata;
        await firstFrame;
        var duration = logic.durationMs(await knownDuration(video));
        var frames = await videoFrames(video, duration);
        return { payload: payload, video: { width: video.videoWidth || 0, height: video.videoHeight || 0, duration_ms: duration }, frames: frames };
      } finally {
        video.removeAttribute("src");
        URL.revokeObjectURL(objectUrl);
      }
    }

    // ---- paste and drop, page-wide ----
    function onPaste(event) {
      if (destroyed || hosted) return;
      var files = clipFiles(event.clipboardData);
      if (!files.length) return; // plain text: the browser pastes it as usual
      var textToo = hasType(event.clipboardData, "text/plain"); // a file copied in Finder also carries its name as text
      var tag = (event.target && event.target.tagName || "").toLowerCase();
      if (!(textToo && (tag === "input" || tag === "textarea"))) event.preventDefault();
      if (panel) { panel.addFiles(files); return; }
      if (sharedPanelOpen()) return;
      openFileDialog({ files: files });
    }
    function dropLabel() { return panel ? "Drop to attach" : "Drop to file a new issue with this attached"; }
    function showDrop() {
      if (!dropHint) {
        dropHint = document.createElement("div");
        dropHint.className = "issue-drop-hint";
        dropHint.innerHTML = '<span class="issue-drop-label"></span>';
        document.body.appendChild(dropHint);
      }
      setTextIfChanged(dropHint.querySelector(".issue-drop-label"), dropLabel());
      document.body.classList.add("issue-dragging-files");
    }
    function hideDrop() {
      dragDepth = 0;
      document.body.classList.remove("issue-dragging-files");
      if (dropHint) dropHint.remove();
      dropHint = null;
    }
    function onDragEnter(event) {
      if (destroyed || hosted || !hasType(event.dataTransfer, "Files")) return;
      dragDepth++;
      showDrop();
    }
    function onDragOver(event) {
      if (destroyed || hosted || !hasType(event.dataTransfer, "Files")) return;
      event.preventDefault();
      try { event.dataTransfer.dropEffect = "copy"; } catch (_error) { /* ignore */ }
      showDrop();
    }
    function onDragLeave(event) {
      if (destroyed || hosted || !hasType(event.dataTransfer, "Files")) return;
      dragDepth = Math.max(0, dragDepth - 1);
      if (!dragDepth) hideDrop();
    }
    function onDrop(event) {
      if (destroyed || hosted) return;
      var transfer = event.dataTransfer;
      if (!hasType(transfer, "Files") && !(transfer && transfer.files && transfer.files.length)) return;
      event.preventDefault();
      hideDrop();
      var files = Array.prototype.filter.call(transfer.files || [], Boolean);
      if (!files.length) return;
      if (panel) { panel.addFiles(files); return; }
      if (sharedPanelOpen()) { if (options.showToast) options.showToast("Close the open panel first, then drop again.", "info"); return; }
      openFileDialog({ files: files });
    }
    function onWindowBlur() { if (dragDepth) hideDrop(); }

    function onGlobalClick(event) {
      if (event.target.closest("#issue-new-button")) openFileDialog();
      if (event.target.closest("[data-action=leave-person]")) leavePerson();
      if (event.target.closest("[data-close]") && !event.target.closest("video")) closeCloser();
    }
    function onResize() { sizeMedia(); }
    function deactivate() {
      active = false;
      requestGeneration++;
      loading = false;
      closeCloser();
      setActive(false);
    }
    function destroy() {
      destroyed = true;
      document.removeEventListener("keydown", onKeydown, true);
      document.removeEventListener("click", onGlobalClick, true);
      document.removeEventListener("paste", onPaste);
      document.removeEventListener("dragenter", onDragEnter);
      document.removeEventListener("dragover", onDragOver);
      document.removeEventListener("dragleave", onDragLeave);
      document.removeEventListener("drop", onDrop);
      window.removeEventListener("resize", onResize);
      window.removeEventListener("blur", onWindowBlur);
      hideDrop();
      closeFileDialog();
      deactivate();
    }

    document.addEventListener("click", onGlobalClick, true);
    document.addEventListener("keydown", onKeydown, true);
    document.addEventListener("paste", onPaste);
    document.addEventListener("dragenter", onDragEnter);
    document.addEventListener("dragover", onDragOver);
    document.addEventListener("dragleave", onDragLeave);
    document.addEventListener("drop", onDrop);
    window.addEventListener("resize", onResize);
    window.addEventListener("blur", onWindowBlur);
    return {
      render: function () { render(); },
      refresh: refresh,
      refreshCount: refreshCount,
      openFile: openFileDialog,
      deactivate: deactivate,
      destroy: destroy
    };
  }

  root.IssueDashboard = { mount: mount };
})(typeof window !== "undefined" ? window : globalThis);
