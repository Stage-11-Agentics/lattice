/**
 * sync.js — Browser-side Automerge CRDT sync via WebSocket relay.
 *
 * Loaded as <script type="module" src="/static/sync.js" defer></script>.
 * Communicates with the main IIFE via the window._lattice bridge.
 *
 * Architecture:
 *   - Each task is an Automerge document held in the browser.
 *   - All string fields use Automerge.RawString for last-writer-wins
 *     merge (prevents text concatenation between independent docs).
 *   - Comments are synced as an Automerge list of RawString JSON blobs.
 *   - All connected browsers share a WebSocket relay (dumb forwarder).
 *   - On local write: update CRDT → send binary to relay.
 *   - On remote message: merge CRDT → persist via apiPost → re-render UI.
 */

var _L = window._lattice || {};
var api = _L.api;
var apiPost = _L.apiPost;

var Automerge = null;
var ws = null;
var docs = {};              // task_id -> Automerge.Doc
var remoteOrigin = {};      // task_id -> true while persisting a remote change
var clientId = crypto.randomUUID();
var relayUrl = null;

// ---- Field classification (mirrors sync/documents.py) ----

// All string fields use RawString (LWW) — independent docs can't safely
// merge collaborative Text without shared history.
var SCALAR_FIELDS = [
  "title", "description",
  "status", "priority", "urgency", "complexity", "type",
  "assigned_to", "created_by", "created_at", "updated_at",
  "done_at", "last_status_changed_at"
];
var LIST_FIELDS = ["tags"];
var META_FIELDS = ["id", "schema_version", "short_id", "last_event_id"];

// ---- Initialization ----

async function init() {
  // 1. Load Automerge (IIFE bundle sets window.Automerge)
  try {
    if (!window.Automerge) {
      await import("/static/automerge.iife.js");
    }
    Automerge = window.Automerge;
    if (!Automerge || !Automerge.init) {
      console.warn("sync.js: Automerge loaded but init() not found");
      return;
    }
  } catch (e) {
    console.warn("sync.js: could not load Automerge:", e);
    return;
  }

  // 2. Fetch relay config
  var syncConfig;
  try {
    syncConfig = await api("/api/sync-config");
  } catch (e) {
    console.warn("sync.js: relay not configured:", e);
    return;
  }
  if (!syncConfig || !syncConfig.enabled) return;
  relayUrl = syncConfig.relay_url;

  // 3. Bootstrap: load current tasks into Automerge docs
  await bootstrapFromServer();

  // 4. Connect to the WebSocket relay
  connectRelay(relayUrl);

  // 5. Install the local-write hook
  window._lattice.onLocalWrite = onLocalWrite;

  console.log("sync.js: ready (" + Object.keys(docs).length + " docs, relay " + relayUrl + ")");
}

// ---- Bootstrap ----

async function bootstrapFromServer() {
  try {
    var tasks = await api("/api/tasks");
    for (var i = 0; i < tasks.length; i++) {
      var t = tasks[i];
      if (!docs[t.id]) {
        var comments = [];
        try {
          comments = await api("/api/tasks/" + t.id + "/comments");
        } catch (e) { /* no comments */ }
        docs[t.id] = taskToDoc(t, comments);
      }
    }
  } catch (e) {
    console.warn("sync.js: bootstrap failed:", e);
  }
}

function taskToDoc(task, comments) {
  var doc = Automerge.init();
  doc = Automerge.change(doc, function(d) {
    // Meta block
    d._meta = {};
    for (var i = 0; i < META_FIELDS.length; i++) {
      var f = META_FIELDS[i];
      if (task[f] != null) {
        d._meta[f] = new Automerge.RawString(String(task[f]));
      }
    }

    // Scalar fields — RawString for last-writer-wins (no concatenation)
    for (var i = 0; i < SCALAR_FIELDS.length; i++) {
      var f = SCALAR_FIELDS[i];
      if (task[f] != null) {
        d[f] = new Automerge.RawString(String(task[f]));
      }
    }

    // List fields
    for (var i = 0; i < LIST_FIELDS.length; i++) {
      var f = LIST_FIELDS[i];
      d[f] = task[f] || [];
    }

    // Comments — stored as list of RawString JSON blobs
    d._comments = [];
    if (comments && comments.length) {
      for (var i = 0; i < comments.length; i++) {
        d._comments.push(new Automerge.RawString(JSON.stringify(comments[i])));
      }
    }
  });
  return doc;
}

// ---- WebSocket relay ----

function connectRelay(url) {
  try {
    ws = new WebSocket(url);
  } catch (e) {
    console.warn("sync.js: WebSocket connect failed:", e);
    scheduleReconnect(url);
    return;
  }

  ws.binaryType = "arraybuffer";

  ws.onopen = function() {
    console.log("sync.js: connected to relay");
    sendAllDocs();
  };

  ws.onmessage = function(event) {
    handleRelayMessage(event.data);
  };

  ws.onclose = function() {
    console.log("sync.js: relay disconnected");
    scheduleReconnect(url);
  };

  ws.onerror = function() {
    // onclose will fire after this
  };
}

function scheduleReconnect(url) {
  setTimeout(function() { connectRelay(url); }, 3000);
}

// ---- Local write hook ----
// Called from _refreshAfterPanelEdit after a successful POST.

async function onLocalWrite(taskId, field, value) {
  if (!Automerge) return;
  if (remoteOrigin[taskId]) return; // don't re-sync our own remote persistence

  // Always fetch full task + comments to rebuild doc
  try {
    var task = (field === "_full" && typeof value === "object") ? value : await api("/api/tasks/" + taskId);
    var comments = [];
    try {
      comments = await api("/api/tasks/" + taskId + "/comments");
    } catch (e) { /* no comments */ }
    docs[taskId] = taskToDoc(task, comments);
    broadcastDoc(taskId);
  } catch (e) {
    // task may have been archived
  }
}

// ---- Sending ----

function broadcastDoc(taskId) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  var doc = docs[taskId];
  if (!doc) return;

  var binary = Automerge.save(doc);
  var msg = JSON.stringify({
    type: "doc_update",
    task_id: taskId,
    doc: uint8ToBase64(binary),
    origin: clientId,
  });
  ws.send(msg);
}

function sendAllDocs() {
  var ids = Object.keys(docs);
  for (var i = 0; i < ids.length; i++) {
    broadcastDoc(ids[i]);
  }
}

// ---- Receiving ----

async function handleRelayMessage(data) {
  var msg;
  try {
    msg = JSON.parse(typeof data === "string" ? data : new TextDecoder().decode(data));
  } catch (e) {
    return;
  }

  // Skip our own messages
  if (msg.origin === clientId) return;

  if (msg.type === "new_peer") {
    // A new browser connected — re-send all our docs so they get our state
    sendAllDocs();
    return;
  }

  if (msg.type === "server_write") {
    // Another server wrote something — just refresh our task list
    await refreshUI();
    return;
  }

  if (msg.type === "doc_update") {
    await handleDocUpdate(msg.task_id, msg.doc);
    return;
  }
}

async function handleDocUpdate(taskId, base64Doc) {
  if (!Automerge) return;

  var binary = base64ToUint8(base64Doc);
  var remoteDoc;
  try {
    remoteDoc = Automerge.load(binary);
  } catch (e) {
    console.warn("sync.js: failed to load remote doc:", e);
    return;
  }

  var localDoc = docs[taskId];
  if (!localDoc) {
    // New task from remote — accept it and persist locally
    docs[taskId] = remoteDoc;
    await persistRemoteTask(taskId, remoteDoc, true);
    await refreshUI();
    return;
  }

  // Merge remote into local.  RawString fields resolve via LWW,
  // Text fields merge character-by-character.
  var merged;
  try {
    merged = Automerge.merge(localDoc, remoteDoc);
  } catch (e) {
    console.warn("sync.js: merge failed:", e);
    return;
  }

  // Detect what changed
  var oldData = docToPlain(localDoc);
  var newData = docToPlain(merged);
  docs[taskId] = merged;

  var changes = diff(oldData, newData);
  var commentsChanged = hasNewComments(oldData._comments || [], newData._comments || []);

  if (changes.length === 0 && !commentsChanged) return;

  // Persist field changes to our local server
  if (changes.length > 0) {
    await persistRemoteChanges(taskId, changes, newData);
  }

  // Persist new comments
  if (commentsChanged) {
    await persistRemoteComments(taskId, oldData._comments || [], newData._comments || []);
  }

  // Update the UI
  await refreshUI();
}

// ---- Persist remote changes to local server ----

async function persistRemoteTask(taskId, doc, isNew) {
  var data = docToPlain(doc);
  remoteOrigin[taskId] = true;
  try {
    if (isNew) {
      await apiPost("/api/tasks", {
        title: data.title || "Untitled",
        status: data.status || "backlog",
        type: data.type || "task",
        priority: data.priority || "medium",
        description: data.description || "",
        tags: data.tags || [],
        actor: "agent:sync-browser",
      });
    }
  } catch (e) {
    console.warn("sync.js: failed to persist remote task:", e);
  } finally {
    delete remoteOrigin[taskId];
  }
}

async function persistRemoteChanges(taskId, changes, newData) {
  remoteOrigin[taskId] = true;
  try {
    // Batch field updates that go to the /update endpoint
    var updateFields = {};
    for (var i = 0; i < changes.length; i++) {
      var c = changes[i];
      if (c.field === "status") {
        try {
          await apiPost("/api/tasks/" + taskId + "/status", {
            status: c.to,
            actor: "agent:sync-browser",
          });
        } catch (e) {
          // Status transition may be invalid
          console.warn("sync.js: status change failed:", e.message);
        }
      } else if (c.field === "assigned_to") {
        await apiPost("/api/tasks/" + taskId + "/assign", {
          assigned_to: c.to || null,
          actor: "agent:sync-browser",
        });
      } else if (c.field === "id" || c.field === "short_id" || c.field === "created_at" || c.field === "created_by" || c.field === "last_status_changed_at" || c.field === "done_at" || c.field === "updated_at" || c.field === "schema_version" || c.field === "last_event_id") {
        // Skip server-managed / immutable fields
        continue;
      } else {
        updateFields[c.field] = c.to;
      }
    }

    if (Object.keys(updateFields).length > 0) {
      await apiPost("/api/tasks/" + taskId + "/update", {
        fields: updateFields,
        actor: "agent:sync-browser",
      });
    }
  } catch (e) {
    console.warn("sync.js: failed to persist remote changes:", e);
  } finally {
    delete remoteOrigin[taskId];
  }
}

// ---- Comment sync ----

function hasNewComments(oldComments, newComments) {
  if (newComments.length <= oldComments.length) return false;
  // Build set of known comment IDs
  var known = {};
  for (var i = 0; i < oldComments.length; i++) {
    if (oldComments[i].id) known[oldComments[i].id] = true;
  }
  for (var i = 0; i < newComments.length; i++) {
    if (newComments[i].id && !known[newComments[i].id]) return true;
  }
  return false;
}

function flattenComments(comments) {
  // Flatten nested comment tree (comments have .replies arrays)
  var flat = [];
  for (var i = 0; i < comments.length; i++) {
    flat.push(comments[i]);
    if (comments[i].replies && comments[i].replies.length) {
      var nested = flattenComments(comments[i].replies);
      for (var j = 0; j < nested.length; j++) flat.push(nested[j]);
    }
  }
  return flat;
}

async function persistRemoteComments(taskId, oldComments, newComments) {
  var oldFlat = flattenComments(oldComments);
  var newFlat = flattenComments(newComments);

  var known = {};
  for (var i = 0; i < oldFlat.length; i++) {
    if (oldFlat[i].id) known[oldFlat[i].id] = true;
  }

  remoteOrigin[taskId] = true;
  try {
    for (var i = 0; i < newFlat.length; i++) {
      var c = newFlat[i];
      if (c.id && !known[c.id] && c.body) {
        try {
          var payload = {
            body: c.body,
            actor: c.actor || "agent:sync-browser",
          };
          if (c.parent_id) payload.parent_id = c.parent_id;
          await apiPost("/api/tasks/" + taskId + "/comment", payload);
        } catch (e) {
          console.warn("sync.js: failed to persist comment:", e.message);
        }
      }
    }
  } finally {
    delete remoteOrigin[taskId];
  }
}

// ---- UI refresh ----

async function refreshUI() {
  if (!_L.api) return;
  try {
    var tasks = await _L.api("/api/tasks");
    if (window._lattice && window._lattice._setTasks) {
      window._lattice._setTasks(tasks);
    }
  } catch (e) {
    // Ignore refresh errors
  }
}

// ---- Helpers ----

function docToPlain(doc) {
  // RawString.toJSON() returns the plain string value,
  // so JSON round-trip correctly unwraps scalar fields.
  try {
    var data = JSON.parse(JSON.stringify(doc));
  } catch (e) {
    return {};
  }

  var result = {};

  // Meta fields
  var meta = data._meta || {};
  for (var i = 0; i < META_FIELDS.length; i++) {
    var f = META_FIELDS[i];
    if (meta[f] != null) result[f] = String(meta[f]);
  }

  // Scalar fields
  for (var i = 0; i < SCALAR_FIELDS.length; i++) {
    var f = SCALAR_FIELDS[i];
    if (data[f] != null) result[f] = String(data[f]);
  }

  // List fields
  for (var i = 0; i < LIST_FIELDS.length; i++) {
    var f = LIST_FIELDS[i];
    if (data[f] != null) result[f] = Array.isArray(data[f]) ? data[f] : [];
  }

  // Comments — stored as RawString JSON blobs, parse them back
  result._comments = [];
  if (data._comments && Array.isArray(data._comments)) {
    for (var i = 0; i < data._comments.length; i++) {
      try {
        result._comments.push(JSON.parse(data._comments[i]));
      } catch (e) { /* skip malformed */ }
    }
  }

  return result;
}

function diff(oldData, newData) {
  var changes = [];
  var keys = {};
  var k;
  for (k in oldData) keys[k] = true;
  for (k in newData) keys[k] = true;
  for (k in keys) {
    if (k === "updated_at" || k === "_comments") continue;
    var ov = JSON.stringify(oldData[k]);
    var nv = JSON.stringify(newData[k]);
    if (ov !== nv) {
      changes.push({ field: k, from: oldData[k], to: newData[k] });
    }
  }
  return changes;
}

function uint8ToBase64(u8) {
  var binary = "";
  for (var i = 0; i < u8.length; i++) {
    binary += String.fromCharCode(u8[i]);
  }
  return btoa(binary);
}

function base64ToUint8(b64) {
  var binary = atob(b64);
  var u8 = new Uint8Array(binary.length);
  for (var i = 0; i < binary.length; i++) {
    u8[i] = binary.charCodeAt(i);
  }
  return u8;
}

// ---- Boot ----

window.addEventListener("load", function() {
  // Delay slightly to ensure window._lattice is populated by the IIFE
  setTimeout(init, 500);
});
