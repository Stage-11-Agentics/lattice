/* Shared shell for the issue-log prototypes (LAT-365): the dashboard nav, the prototype bar,
   toasts, formatters, the quick-file panel, the dialogs every page can reuse, and photo and
   video intake (paste, drop, choose).
   Requires mock-media.js (optional), mock-data.js and store.js. */
(function () {
  "use strict";

  var S = window.IssueStore;
  var PAGES = [
    { key: "index", href: "index.html", label: "Overview" },
    { key: "filing", href: "filing.html", label: "Filing" },
    { key: "a", href: "take-a-inbox.html", label: "A Inbox" },
    { key: "b", href: "take-b-ledger.html", label: "B Ledger (round 1)" },
    { key: "c", href: "take-c-board.html", label: "C Board (round 1)" }
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
  function bytes(n) {
    if (n == null) { return "—"; }
    if (n < 1e6) { return Math.max(1, Math.round(n / 1000)) + " KB"; }
    return (n / 1e6).toFixed(1) + " MB";
  }
  // 7.5 -> "0:07"; with tenths -> "0:07.5"
  function dur(s, tenths) {
    if (s == null || !isFinite(s)) { return tenths ? "0:00.0" : "0:00"; }
    s = Math.max(0, s);
    var m = Math.floor(s / 60), r = s - m * 60;
    var sec = tenths ? r.toFixed(1) : String(Math.floor(r));
    if (r < 10) { sec = "0" + sec; }
    return m + ":" + sec;
  }

  var fmt = {
    esc: esc, rel: rel, abs: abs, bytes: bytes, dur: dur,
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
    firstLine: function (t, n) { var l = String(t).split("\n")[0]; n = n || 120; return l.length > n ? l.slice(0, n - 3) + "..." : l; },
    // "1 photo", "2 photos and 1 video"
    mediaCount: function (list) {
      var p = 0, v = 0;
      (list || []).forEach(function (m) { if (m.kind === "video") { v++; } else { p++; } });
      var parts = [];
      if (p) { parts.push(p + (p === 1 ? " photo" : " photos")); }
      if (v) { parts.push(v + (v === 1 ? " video" : " videos")); }
      return parts.join(" and ");
    }
  };

  // ---- toasts ----
  var toastBox;
  function toast(msg, kind) {
    if (!toastBox) { return; }
    var t = el('<div class="toast' + (kind ? " toast-" + kind : "") + '"></div>');
    t.innerHTML = msg;
    toastBox.appendChild(t);
    setTimeout(function () { if (t.parentNode) { t.parentNode.removeChild(t); } }, 4200);
  }
  function guard(fn) {
    try { return fn(); } catch (e) { toast(esc((e.code ? e.code + ": " : "") + e.message), "error"); return undefined; }
  }

  // ---- dialogs ----
  // One overlay at a time. Pages that need a dialog build it with Shell.dialog, so Shell.dialogOpen()
  // is the single answer to "should page shortcuts be ignored right now".
  var openOverlay = null;
  function closeDialog() {
    if (!openOverlay) { return; }
    var o = openOverlay;
    openOverlay = null;
    if (o._onClose) { try { o._onClose(); } catch (e) { /* ignore */ } }
    if (o.parentNode) { o.parentNode.removeChild(o); }
  }
  function dialog(title, bodyHtml, footHtml, opts) {
    opts = opts || {};
    var o = el('<div class="overlay"><div class="dialog' + (opts.cls ? " " + opts.cls : "") + '" role="dialog" tabindex="-1" aria-label="' + esc(title) + '">' +
      '<div class="dialog-head"><span>' + title + '</span><button class="btn btn-sm" data-x>Esc</button></div>' +
      '<div class="dialog-body">' + bodyHtml + '</div><div class="dialog-foot">' + footHtml + "</div></div></div>");
    closeDialog();
    o.addEventListener("mousedown", function (e) { if (e.target === o) { closeDialog(); } });
    o.querySelector("[data-x]").onclick = closeDialog;
    o.addEventListener("keydown", function (e) { if (e.key === "Escape") { e.stopPropagation(); closeDialog(); } });
    document.body.appendChild(o);
    openOverlay = o;
    return o;
  }
  function dialogOpen() {
    return !!openOverlay;
  }

  // ---- photos and video: reading files ----
  function kindOf(file) {
    var t = (file && file.type) || "", n = ((file && file.name) || "").toLowerCase();
    if (/^image\//.test(t) || /\.(png|jpe?g|gif|webp|heic|avif)$/.test(n)) { return "photo"; }
    if (/^video\//.test(t) || /\.(mp4|mov|m4v|webm|mkv)$/.test(n)) { return "video"; }
    return null;
  }
  function mediaFiles(list) {
    var out = [];
    Array.prototype.forEach.call(list || [], function (f) { if (f) { out.push(f); } });
    return out;
  }
  // A File becomes a media item held in memory: an object URL, its size and shape, and for a video
  // its duration and a poster drawn by seeking the video and painting one frame to a canvas.
  // Never rejects; an unreadable file resolves with broken: true.
  function readMedia(file, hint) {
    hint = hint || {};
    return new Promise(function (resolve) {
      var kind = kindOf(file);
      var item = {
        kind: kind || "file", name: file.name || (kind === "video" ? "Recording.webm" : "Pasted image.png"),
        bytes: file.size || 0, local: true, frames: [], added_via: hint.via || "file"
      };
      if (!kind) { resolve(item); return; }
      try { item.src = URL.createObjectURL(file); } catch (e) { item.broken = true; resolve(item); return; }
      var settled = false;
      function finish() { if (!settled) { settled = true; resolve(item); } }
      setTimeout(function () { if (!settled) { item.slow = true; finish(); } }, 6000);
      if (kind === "photo") {
        var img = new Image();
        img.onload = function () { item.w = img.naturalWidth; item.h = img.naturalHeight; finish(); };
        img.onerror = function () { item.broken = true; finish(); };
        img.src = item.src;
        return;
      }
      var v = document.createElement("video");
      v.muted = true; v.preload = "auto"; v.playsInline = true;
      v.onloadedmetadata = function () {
        item.w = v.videoWidth; item.h = v.videoHeight;
        var d = isFinite(v.duration) ? v.duration : hint.duration;
        item.duration = d ? Math.round(d * 10) / 10 : null;
        try { v.currentTime = d ? Math.min(1, d / 4) : 0.2; } catch (e) { finish(); }
      };
      v.onseeked = function () {
        try {
          var w = v.videoWidth || 640, h = v.videoHeight || 400, sc = Math.min(1, 640 / w);
          var c = document.createElement("canvas");
          c.width = Math.round(w * sc); c.height = Math.round(h * sc);
          c.getContext("2d").drawImage(v, 0, 0, c.width, c.height);
          item.poster = c.toDataURL("image/jpeg", 0.8);
        } catch (e) { /* no poster: the tile shows the kind instead */ }
        finish();
      };
      v.onerror = function () { item.broken = true; finish(); };
      v.src = item.src;
    });
  }
  function clipFiles(cd) {
    var out = [];
    if (!cd) { return out; }
    if (cd.items && cd.items.length) {
      Array.prototype.forEach.call(cd.items, function (it) { if (it.kind === "file") { var f = it.getAsFile(); if (f) { out.push(f); } } });
    }
    if (!out.length && cd.files) { out = mediaFiles(cd.files); }
    return out;
  }
  function hasType(dt, type) { return !!(dt && dt.types && Array.prototype.indexOf.call(dt.types, type) >= 0); }

  // ---- the quick-file panel ----
  // One field is required: the text. Photos and video are first-class but optional.
  var panel = null; // { addFiles(files), overlay }
  function openFile(prefill) {
    if (typeof prefill === "string") { prefill = { text: prefill }; }
    prefill = prefill || {};
    if (panel && openOverlay === panel.overlay) {
      if (prefill.files) { panel.addFiles(prefill.files, "drop"); }
      return;
    }
    var o = dialog("File an issue",
      '<label for="fi-text">What did you see?</label>' +
      '<textarea id="fi-text" placeholder="The footer overlaps the Complete button at 400px wide"></textarea>' +
      '<div class="err" id="fi-err"></div>' +
      '<div class="fi-media-head"><label>Photos and video <span class="muted">(optional)</span></label><span class="fi-total" id="fi-total"></span></div>' +
      '<div class="tray" id="fi-tray"></div>' +
      '<div class="fi-media-actions">' +
        '<button type="button" class="btn fi-choose" id="fi-choose">Choose a file</button>' +
        '<input type="file" id="fi-input" accept="image/*,video/*" multiple hidden>' +
      "</div>",
      '<span class="hint"><kbd>⌘</kbd> <kbd>V</kbd> pastes a screenshot. <kbd>⌘</kbd> <kbd>Enter</kbd> files it. No story is made.</span>' +
      '<button class="btn btn-primary" data-go>File issue</button>',
      { cls: "dialog-file" });
    if (!o.parentNode) { return; } // refused while recording
    var text = o.querySelector("#fi-text"), err = o.querySelector("#fi-err");
    var trayEl = o.querySelector("#fi-tray"), input = o.querySelector("#fi-input");
    var tray = []; // { state: "reading" | "ok" | "refused", item, reason, name, bytes }
    if (prefill.text) { text.value = prefill.text; }

    function accepted() { return tray.filter(function (t) { return t.state === "ok"; }).map(function (t) { return t.item; }); }
    function drawTray() {
      var total = accepted().reduce(function (a, m) { return a + m.bytes; }, 0);
      o.querySelector("#fi-total").textContent = bytes(total) + " of " + bytes(S.LIMITS.issue).replace(".0 ", " ");
      if (!tray.length) {
        trayEl.innerHTML = '<div class="tray-empty"><b>Paste, drop or choose a photo or video</b><span>A screenshot on the clipboard goes in with <kbd>⌘</kbd> <kbd>V</kbd>.</span></div>';
        return;
      }
      trayEl.innerHTML = tray.map(function (t, n) {
        var m = t.item || {};
        var thumb = t.state === "refused" ? '<div class="tile-thumb refused">Not attached</div>'
          : t.state === "reading" ? '<div class="tile-thumb">Reading...</div>'
          : (m.kind === "photo" && !m.broken) ? '<div class="tile-thumb"><img src="' + esc(m.src) + '" alt=""></div>'
          : m.poster ? '<div class="tile-thumb"><img src="' + esc(m.poster) + '" alt=""><span class="play">▶</span></div>'
          : '<div class="tile-thumb">' + (m.kind === "video" ? "video" : "photo") + "</div>";
        var meta = t.state === "refused" ? '<span class="tile-why">' + esc(t.reason) + "</span>"
          : '<span class="tile-kind">' + (m.kind === "video" ? "video " + dur(m.duration) : "photo") + '</span><span class="tile-size">' + bytes(t.bytes) + "</span>";
        return '<div class="tile' + (t.state === "refused" ? " tile-refused" : "") + '" title="' + esc(t.name) + '">' + thumb +
          '<div class="tile-meta">' + meta + '</div><button type="button" class="tile-x" data-n="' + n + '" title="Remove">×</button></div>';
      }).join("");
      trayEl.querySelectorAll(".tile-x").forEach(function (b) {
        b.onclick = function () {
          var t = tray[+b.getAttribute("data-n")];
          if (t && t.item && t.item.src && t.item.local) { try { URL.revokeObjectURL(t.item.src); } catch (e) { /* ignore */ } }
          tray.splice(+b.getAttribute("data-n"), 1); drawTray();
        };
      });
    }
    function addFiles(files, via, hint) {
      err.textContent = "";
      mediaFiles(files).forEach(function (f) {
        var entry = { state: "reading", name: f.name || "file", bytes: f.size || 0 };
        var why = kindOf(f) ? S.mediaProblem(accepted(), { kind: kindOf(f), bytes: f.size || 0 }) : "Only photos and videos can be attached.";
        if (why) { entry.state = "refused"; entry.reason = why; tray.push(entry); return; }
        entry.item = { kind: kindOf(f), bytes: f.size || 0 };
        tray.push(entry);
        readMedia(f, { via: via, duration: hint && hint.duration }).then(function (m) {
          entry.item = m;
          if (m.broken) { entry.state = "refused"; entry.reason = "This file could not be read as a " + (m.kind === "video" ? "video" : "photo") + "."; }
          else { entry.state = "ok"; }
          drawTray();
        });
      });
      drawTray();
    }

    o.querySelector("#fi-choose").onclick = function () { input.click(); };
    input.onchange = function () { addFiles(input.files, "choose"); input.value = ""; text.focus(); };
    function go() {
      err.textContent = "";
      var reading = tray.filter(function (t) { return t.state === "reading"; }).length;
      if (reading) { err.textContent = "Still reading " + (reading === 1 ? "one file" : reading + " files") + ". A moment."; return; }
      var media = accepted();
      if (!text.value.trim() && media.length) {
        err.textContent = "Add a line saying what the " + (media.length === 1 ? media[0].kind : "attachments") + " show" + (media.length === 1 ? "s" : "") +
          ". Without words, a queue of screenshots cannot be scanned or searched, by a person or an agent.";
        text.focus(); return;
      }
      try {
        var issue = S.file({ text: text.value, media: media });
        toast("Filed " + fmt.iid(issue.id) + ": " + esc(fmt.firstLine(issue.text, 70)) + (media.length ? " (" + fmt.mediaCount(issue.media) + ")" : ""));
        tray = []; // filed items now belong to the issue; their object URLs stay alive
        closeDialog();
      } catch (e) { err.textContent = e.message; text.focus(); }
    }
    o._onClose = function () {
      tray.forEach(function (t) { if (t.item && t.item.src && t.item.local) { try { URL.revokeObjectURL(t.item.src); } catch (e) { /* ignore */ } } });
      panel = null;
    };
    o.querySelector("[data-go]").onclick = go;
    o.addEventListener("keydown", function (e) { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); go(); } });
    panel = { overlay: o, addFiles: addFiles, tray: function () { return tray; } };
    drawTray();
    if (prefill.files) { addFiles(prefill.files, prefill.via || "drop"); }
    text.focus();
  }

  // ---- paste and drop, page-wide ----
  document.addEventListener("paste", function (e) {
    var files = clipFiles(e.clipboardData);
    if (!files.length) { return; }                         // plain text: the browser pastes it as usual
    var textToo = hasType(e.clipboardData, "text/plain");  // e.g. a file copied in Finder also carries its name as text
    var tag = (e.target && e.target.tagName || "").toLowerCase();
    var inField = tag === "input" || tag === "textarea";
    if (!(textToo && inField)) { e.preventDefault(); }
    if (panel && openOverlay === panel.overlay) { panel.addFiles(files, "paste"); return; }
    if (openOverlay) { return; }
    openFile({ files: files, via: "paste" });
  });

  var dragDepth = 0, dropHint = null;
  function dropLabel() {
    return panel && openOverlay === panel.overlay ? "Drop to attach" : "Drop to file a new issue with this attached";
  }
  function showDrop() {
    if (!dropHint) {
      dropHint = el('<div class="drop-hint"><span class="drop-label"></span></div>');
      document.body.appendChild(dropHint);
    }
    dropHint.querySelector(".drop-label").textContent = dropLabel();
    document.body.classList.add("dragging-files");
  }
  function hideDrop() {
    dragDepth = 0;
    document.body.classList.remove("dragging-files");
    if (dropHint && dropHint.parentNode) { dropHint.parentNode.removeChild(dropHint); }
    dropHint = null;
  }
  document.addEventListener("dragenter", function (e) { if (!hasType(e.dataTransfer, "Files")) { return; } dragDepth++; showDrop(); });
  document.addEventListener("dragover", function (e) {
    if (!hasType(e.dataTransfer, "Files")) { return; }
    e.preventDefault();
    try { e.dataTransfer.dropEffect = "copy"; } catch (x) { /* ignore */ }
    showDrop();
  });
  document.addEventListener("dragleave", function (e) {
    if (!hasType(e.dataTransfer, "Files")) { return; }
    dragDepth = Math.max(0, dragDepth - 1);
    if (!dragDepth) { hideDrop(); }
  });
  document.addEventListener("drop", function (e) {
    if (!hasType(e.dataTransfer, "Files") && !(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length)) { return; }
    e.preventDefault();
    hideDrop();
    var files = mediaFiles(e.dataTransfer.files);
    if (!files.length) { return; }
    if (panel && openOverlay === panel.overlay) { panel.addFiles(files, "drop"); return; }
    if (openOverlay) { toast("Close the open dialog first, then drop again.", "info"); return; }
    openFile({ files: files, via: "drop" });
  });
  window.addEventListener("blur", function () { if (dragDepth) { hideDrop(); } });


  // ---- pickers and the decision dialogs ----
  function picker(title, rowsFn, hint, foot) {
    return new Promise(function (resolve) {
      var o = dialog(title,
        '<input type="text" id="pk-q" placeholder="' + esc(hint) + '" autocomplete="off"><div class="pick-list" id="pk-list"></div>',
        '<span class="hint"><kbd>↑</kbd> <kbd>↓</kbd> move, <kbd>Enter</kbd> picks</span>' + (foot || "") + '<button class="btn" data-cancel>Cancel</button>');
      if (!o.parentNode) { resolve(null); return; }
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
        return i.id !== issueId && !(i.closure && i.closure.kind === "duplicate") &&
          (i.id + " " + i.text).toLowerCase().indexOf(q) >= 0;
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
      if (!o.parentNode) { resolve(null); return; }
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
      if (!o.parentNode) { resolve(false); return; }
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
    var ok = guard(function () { S.unlink(issueId, taskId); return true; });
    if (ok) { toast("Unlinked " + fmt.iid(issueId) + " from " + fmt.tid(taskId), "info"); }
    return !!ok;
  }

  // ---- mount ----
  function mount(opts) {
    opts = opts || {};
    var active = opts.active || "issues";
    if (opts.title) { document.title = opts.title; }
    var tabs = [["board", "Board"], ["list", "List"], ["issues", "Issues"], ["activity", "Activity"], ["stats", "Stats"], ["cube", "Cube"], ["web", "Web"]];
    if (opts.hideIssuesTab) { tabs = tabs.filter(function (t) { return t[0] !== "issues"; }); }
    var nav = el('<div class="nav"><span class="nav-brand">Lattice</span>' + tabs.map(function (t) {
      return '<span class="nav-tab' + (t[0] === active ? " active" : "") + '" data-view="' + t[0] + '">' +
        (t[0] === "issues" ? '<span class="nav-tab-label">Issues</span><span class="nav-count" id="nav-issue-count"></span>' : t[1]) + "</span>";
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
    var bar = el('<div class="proto-bar"><strong>PROTOTYPE</strong><span class="proto-note">Mock data. Nothing is saved; reload resets it.</span>' +
      PAGES.map(function (p) { return '<a href="' + p.href + '"' + (p.key === here ? ' class="here"' : "") + ">" + p.label + "</a>"; }).join("") +
      '<span class="spacer"></span>' +
      '<button type="button" id="proto-reset">Reset data</button></div>');
    document.body.appendChild(bar);
    bar.querySelector("#proto-reset").onclick = function () { location.reload(); };

    function count() {
      var n = document.getElementById("nav-issue-count");
      if (n) { n.textContent = S.counts().open; n.title = S.counts().open + " issues have no story yet"; }
    }
    S.subscribe(count); count();

    document.addEventListener("keydown", function (e) {
      if (dialogOpen() || e.metaKey || e.ctrlKey || e.altKey) { return; }
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
    dialog: dialog, closeDialog: closeDialog,
    dialogOpen: dialogOpen,
    // the name
    // photos and video
    readMedia: readMedia, kindOf: kindOf,
    filePanel: function () { return panel && openOverlay === panel.overlay ? panel : null; }
  };
})();
