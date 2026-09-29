/* Shared shell for the issue-log prototypes (LAT-365): the dashboard nav, the prototype bar,
   toasts, formatters, the quick-file panel, and the dialogs every take can reuse.
   Requires mock-data.js and store.js. */
(function () {
  "use strict";

  var S = window.IssueStore;
  var PAGES = [
    { key: "index", href: "index.html", label: "Overview" },
    { key: "filing", href: "filing.html", label: "Filing" },
    { key: "a", href: "take-a-inbox.html", label: "A Inbox" },
    { key: "b", href: "take-b-ledger.html", label: "B Ledger" },
    { key: "c", href: "take-c-board.html", label: "C On the board" }
  ];

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function el(html) { var d = document.createElement("div"); d.innerHTML = html.trim(); return d.firstChild; }

  function rel(ts) {
    var s = Math.max(0, Math.round((S.now() - Date.parse(ts)) / 1000));
    if (s < 60) { return "just now"; }
    var m = Math.round(s / 60); if (m < 60) { return m + "m ago"; }
    var h = Math.round(m / 60); if (h < 36) { return h + "h ago"; }
    return Math.round(h / 24) + "d ago";
  }
  function abs(ts) { return ts.replace("T", " ").replace(/:\d\dZ$/, " UTC"); }

  var fmt = {
    esc: esc, rel: rel, abs: abs,
    actor: function (a) {
      if (!a) { return '<span class="muted">unassigned</span>'; }
      var kind = a.indexOf("human:") === 0 ? "human" : "agent";
      return '<span class="actor actor-' + kind + '">' + esc(a) + "</span>";
    },
    state: function (st) { return '<span class="state state-' + st + '">' + st + "</span>"; },
    tstatus: function (st) { return '<span class="tstatus tstatus-' + esc(st) + '">' + esc(String(st).replace(/_/g, " ")) + "</span>"; },
    conf: function (c) { return '<span class="conf conf-' + esc(c || "none") + '">' + esc(c || "—") + "</span>"; },
    iid: function (id) { return '<span class="iid">' + esc(id) + "</span>"; },
    tid: function (id) { return '<span class="tid">' + esc(id) + "</span>"; },
    firstLine: function (t, n) { var l = String(t).split("\n")[0]; n = n || 120; return l.length > n ? l.slice(0, n - 3) + "..." : l; }
  };

  // ---- toasts ----
  var toastBox;
  function toast(msg, kind) {
    var t = el('<div class="toast' + (kind ? " toast-" + kind : "") + '"></div>');
    t.innerHTML = msg;
    toastBox.appendChild(t);
    setTimeout(function () { if (t.parentNode) { t.parentNode.removeChild(t); } }, 4200);
  }
  function guard(fn) {
    try { return fn(); } catch (e) { toast(esc((e.code ? e.code + ": " : "") + e.message), "error"); return undefined; }
  }

  // ---- dialogs ----
  var openOverlay = null;
  function closeDialog() { if (openOverlay) { document.body.removeChild(openOverlay); openOverlay = null; } }
  function dialog(title, bodyHtml, footHtml) {
    closeDialog();
    var o = el('<div class="overlay"><div class="dialog" role="dialog" aria-label="' + esc(title) + '">' +
      '<div class="dialog-head"><span>' + title + '</span><button class="btn btn-sm" data-x>Esc</button></div>' +
      '<div class="dialog-body">' + bodyHtml + '</div><div class="dialog-foot">' + footHtml + "</div></div></div>");
    o.addEventListener("mousedown", function (e) { if (e.target === o) { closeDialog(); } });
    o.querySelector("[data-x]").onclick = closeDialog;
    o.addEventListener("keydown", function (e) { if (e.key === "Escape") { e.stopPropagation(); closeDialog(); } });
    document.body.appendChild(o);
    openOverlay = o;
    return o;
  }

  // File an issue: one field. Everything else is optional and out of the way.
  function openFile(prefill) {
    var o = dialog("File an issue",
      '<label for="fi-text">What did you see?</label>' +
      '<textarea id="fi-text" placeholder="The footer overlaps the Complete button at 400px wide"></textarea>' +
      '<div class="err" id="fi-err"></div>' +
      '<label>How sure are you? <span class="muted">(optional)</span></label>' +
      '<div class="seg" id="fi-conf"><button type="button" data-v="" aria-pressed="true">not said</button>' +
      '<button type="button" data-v="possible" aria-pressed="false">possible</button>' +
      '<button type="button" data-v="definite" aria-pressed="false">definite</button></div>' +
      '<label for="fi-ev">Evidence <span class="muted">(optional: a path or a URL)</span></label>' +
      '<input type="text" id="fi-ev" placeholder="evidence/2026-09-29/footer-400.png">',
      '<span class="hint"><kbd>⌘</kbd> <kbd>Enter</kbd> files it. No story is made, nothing is planned.</span>' +
      '<button class="btn" data-more>File and add another</button><button class="btn btn-primary" data-go>File issue</button>');
    var text = o.querySelector("#fi-text"), err = o.querySelector("#fi-err"), ev = o.querySelector("#fi-ev"), conf = "";
    if (prefill) { text.value = prefill; }
    o.querySelectorAll("#fi-conf button").forEach(function (b) {
      b.onclick = function () {
        conf = b.getAttribute("data-v");
        o.querySelectorAll("#fi-conf button").forEach(function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
      };
    });
    function go(again) {
      err.textContent = "";
      try {
        var issue = S.file({ text: text.value, confidence: conf || null, evidence: ev.value.trim() ? [ev.value.trim()] : [] });
        toast("Filed " + fmt.iid(issue.id) + ": " + esc(fmt.firstLine(issue.text, 70)));
        if (again) { text.value = ""; ev.value = ""; text.focus(); } else { closeDialog(); }
      } catch (e) { err.textContent = e.message; text.focus(); }
    }
    o.querySelector("[data-go]").onclick = function () { go(false); };
    o.querySelector("[data-more]").onclick = function () { go(true); };
    o.addEventListener("keydown", function (e) { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); go(false); } });
    text.focus();
  }

  function picker(title, rowsFn, hint, foot) {
    return new Promise(function (resolve) {
      var o = dialog(title,
        '<input type="text" id="pk-q" placeholder="' + esc(hint) + '" autocomplete="off"><div class="pick-list" id="pk-list"></div>',
        '<span class="hint"><kbd>↑</kbd> <kbd>↓</kbd> move, <kbd>Enter</kbd> picks</span>' + (foot || "") + '<button class="btn" data-cancel>Cancel</button>');
      var q = o.querySelector("#pk-q"), list = o.querySelector("#pk-list"), cursor = 0, rows = [];
      function draw() {
        rows = rowsFn(q.value.trim().toLowerCase());
        cursor = Math.min(cursor, Math.max(0, rows.length - 1));
        list.innerHTML = rows.length ? rows.map(function (r, n) {
          return '<div class="pick-row' + (n === cursor ? " cursor" : "") + '" data-n="' + n + '">' + r.html + "</div>";
        }).join("") : '<div class="pick-empty">Nothing matches.</div>';
        list.querySelectorAll(".pick-row").forEach(function (row) {
          row.onclick = function () { done(rows[+row.getAttribute("data-n")].value); };
        });
        var c = list.querySelector(".cursor"); if (c) { c.scrollIntoView({ block: "nearest" }); }
      }
      function done(v) { closeDialog(); resolve(v); }
      q.oninput = function () { cursor = 0; draw(); };
      o.addEventListener("keydown", function (e) {
        if (e.key === "ArrowDown") { e.preventDefault(); cursor = Math.min(rows.length - 1, cursor + 1); draw(); }
        else if (e.key === "ArrowUp") { e.preventDefault(); cursor = Math.max(0, cursor - 1); draw(); }
        else if (e.key === "Enter" && rows[cursor]) { e.preventDefault(); done(rows[cursor].value); }
      });
      o.querySelector("[data-cancel]").onclick = function () { done(null); };
      o.querySelector("[data-x]").onclick = function () { done(null); };
      draw(); q.focus();
    });
  }

  function storyRows(q, skip) {
    return S.tasks().filter(function (t) {
      return t.status !== "cancelled" && skip.indexOf(t.id) < 0 && (t.id + " " + t.title).toLowerCase().indexOf(q) >= 0;
    }).sort(function (a, b) { return a.id < b.id ? 1 : -1; }).map(function (t) {
      return { value: t.id, html: fmt.tid(t.id) + fmt.tstatus(t.status) + '<span class="grow">' + esc(t.title) + "</span>" };
    });
  }

  // Link an issue to a story that already exists. Resolves to the story id, or null.
  function linkDialog(issueId) {
    var issue = S.get(issueId);
    var skip = issue.links.map(function (l) { return l.task_id; });
    return picker("Link " + esc(issueId) + " to an existing story", function (q) { return storyRows(q, skip); }, "Search stories by ID or title")
      .then(function (taskId) {
        if (!taskId) { return null; }
        var r = guard(function () { return S.link(issueId, taskId); });
        if (r) { toast("Linked " + fmt.iid(issueId) + " to " + fmt.tid(taskId) + " " + fmt.tstatus(S.task(taskId).status)); }
        return r ? taskId : null;
      });
  }

  function duplicateDialog(issueId) {
    return picker("Mark " + esc(issueId) + " as a duplicate of", function (q) {
      return S.issues().filter(function (i) {
        return i.id !== issueId && !(i.closure && i.closure.kind === "duplicate") && (i.id + " " + i.text).toLowerCase().indexOf(q) >= 0;
      }).reverse().map(function (i) {
        return { value: i.id, html: fmt.iid(i.id) + fmt.state(S.state(i)) + '<span class="grow">' + esc(fmt.firstLine(i.text)) + "</span>" };
      });
    }, "Search issues by ID or text").then(function (ofId) {
      if (!ofId) { return null; }
      var ok = guard(function () { S.duplicate(issueId, ofId); return true; });
      if (ok) { toast("Marked " + fmt.iid(issueId) + " as a duplicate of " + fmt.iid(ofId)); }
      return ok ? ofId : null;
    });
  }

  // Make one story from one or more issues. Resolves to the new story, or null.
  function promoteDialog(ids) {
    return new Promise(function (resolve) {
      var picked = ids.map(S.get);
      var o = dialog("Make a story from " + (ids.length === 1 ? esc(ids[0]) : ids.length + " issues"),
        '<label>From</label><div class="promote-from">' + picked.map(function (i) {
          return "<div>" + fmt.iid(i.id) + '<span class="grow">' + esc(fmt.firstLine(i.text)) + "</span></div>";
        }).join("") + "</div>" +
        '<label for="pr-title">Story title</label><input type="text" id="pr-title">' +
        '<div class="err" id="pr-err"></div>' +
        '<label>Priority</label><div class="seg" id="pr-pri">' + ["low", "medium", "high", "critical"].map(function (p) {
          return '<button type="button" data-v="' + p + '" aria-pressed="' + (p === "medium") + '">' + p + "</button>";
        }).join("") + "</div>",
        '<span class="hint">The story starts in backlog. Planning happens later, in the usual way.</span>' +
        '<button class="btn" data-cancel>Cancel</button><button class="btn btn-primary" data-go>Make story</button>');
      var title = o.querySelector("#pr-title"), pri = "medium";
      title.value = fmt.firstLine(picked[0].text);
      o.querySelectorAll("#pr-pri button").forEach(function (b) {
        b.onclick = function () {
          pri = b.getAttribute("data-v");
          o.querySelectorAll("#pr-pri button").forEach(function (x) { x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
        };
      });
      function finish(v) { closeDialog(); resolve(v); }
      function go() {
        if (!title.value.trim()) { o.querySelector("#pr-err").textContent = "A story needs a title."; return; }
        var t = guard(function () { return S.promote(ids, { title: title.value, priority: pri }); });
        if (t) { toast("Created " + fmt.tid(t.id) + " from " + ids.map(fmt.iid).join(", ")); }
        finish(t || null);
      }
      o.querySelector("[data-go]").onclick = go;
      o.querySelector("[data-cancel]").onclick = function () { finish(null); };
      o.querySelector("[data-x]").onclick = function () { finish(null); };
      o.addEventListener("keydown", function (e) { if (e.key === "Enter" && e.target === title) { e.preventDefault(); go(); } });
      title.focus(); title.select();
    });
  }

  function dismissDialog(issueId) {
    return new Promise(function (resolve) {
      var o = dialog("Dismiss " + esc(issueId),
        '<label for="ds-r">Why? <span class="muted">(required; the filer will read this)</span></label>' +
        '<textarea id="ds-r" style="min-height:4.5rem" placeholder="Not reproducible on the current build"></textarea><div class="err" id="ds-err"></div>',
        '<span class="hint">Nothing is deleted. A dismissed issue can be reopened.</span>' +
        '<button class="btn" data-cancel>Cancel</button><button class="btn btn-danger" data-go>Dismiss</button>');
      var r = o.querySelector("#ds-r");
      function finish(v) { closeDialog(); resolve(v); }
      function go() {
        try { S.dismiss(issueId, r.value); toast("Dismissed " + fmt.iid(issueId)); finish(true); }
        catch (e) { o.querySelector("#ds-err").textContent = e.message; r.focus(); }
      }
      o.querySelector("[data-go]").onclick = go;
      o.querySelector("[data-cancel]").onclick = function () { finish(false); };
      o.querySelector("[data-x]").onclick = function () { finish(false); };
      o.addEventListener("keydown", function (e) { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); go(); } });
      r.focus();
    });
  }

  function reopen(issueId) {
    var ok = guard(function () { S.reopen(issueId); return true; });
    if (ok) { toast("Reopened " + fmt.iid(issueId)); }
    return !!ok;
  }
  function unlink(issueId, taskId) {
    guard(function () { S.unlink(issueId, taskId); });
    toast("Unlinked " + fmt.iid(issueId) + " from " + fmt.tid(taskId), "info");
  }

  // ---- mount ----
  function mount(opts) {
    opts = opts || {};
    var active = opts.active || "issues";
    var tabs = [["board", "Board"], ["list", "List"], ["issues", "Issues"], ["activity", "Activity"], ["stats", "Stats"], ["cube", "Cube"], ["web", "Web"]];
    if (opts.hideIssuesTab) { tabs = tabs.filter(function (t) { return t[0] !== "issues"; }); }
    var nav = el('<div class="nav"><span class="nav-brand">Lattice</span>' + tabs.map(function (t) {
      return '<span class="nav-tab' + (t[0] === active ? " active" : "") + '" data-view="' + t[0] + '">' + t[1] +
        (t[0] === "issues" ? '<span class="nav-count" id="nav-issue-count"></span>' : "") + "</span>";
    }).join("") + '<div class="nav-right"><button type="button" class="btn btn-issue" id="file-issue-btn" title="File an issue (i)">+ Issue</button>' +
      '<button type="button" class="btn btn-primary" id="new-task-btn">+ New Task</button></div></div>');
    document.body.insertBefore(nav, document.body.firstChild);
    nav.querySelectorAll(".nav-tab").forEach(function (t) {
      t.onclick = function () {
        var v = t.getAttribute("data-view");
        if (v === active) { return; }
        if (opts.onTab && opts.onTab(v)) { return; }
        toast("The " + esc(t.textContent.replace(/\d+/g, "").trim()) + " tab is the existing dashboard and is not part of this prototype.", "info");
      };
    });
    nav.querySelector("#file-issue-btn").onclick = function () { openFile(); };
    nav.querySelector("#new-task-btn").onclick = function () { toast("New Task is the existing dashboard dialog and is not part of this prototype.", "info"); };

    toastBox = el('<div class="toast-container"></div>');
    document.body.appendChild(toastBox);

    var here = opts.page || "";
    var bar = el('<div class="proto-bar"><strong>PROTOTYPE</strong><span>Mock data. Nothing is saved; reload resets it.</span>' +
      PAGES.map(function (p) { return '<a href="' + p.href + '"' + (p.key === here ? ' class="here"' : "") + ">" + p.label + "</a>"; }).join("") +
      '<span class="spacer"></span><button type="button" id="proto-reset">Reset data</button></div>');
    document.body.appendChild(bar);
    bar.querySelector("#proto-reset").onclick = function () { location.reload(); };

    function count() {
      var n = document.getElementById("nav-issue-count");
      if (n) { n.textContent = S.counts().open; n.title = S.counts().open + " issues have no story yet"; }
    }
    S.subscribe(count); count();

    document.addEventListener("keydown", function (e) {
      if (openOverlay || e.metaKey || e.ctrlKey || e.altKey) { return; }
      var tag = (e.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") { return; }
      if (e.key === "i") { e.preventDefault(); openFile(); }
    });

    // Deep link for demos and screenshots: ?file=1 opens the quick-file panel on load.
    if (/[?&]file=1(&|$)/.test(location.search)) { openFile(); }
  }

  window.Shell = {
    mount: mount, toast: toast, guard: guard, fmt: fmt, el: el,
    openFile: openFile, promoteDialog: promoteDialog, linkDialog: linkDialog,
    dismissDialog: dismissDialog, duplicateDialog: duplicateDialog, reopen: reopen, unlink: unlink,
    dialogOpen: function () { return !!openOverlay; }
  };
})();
