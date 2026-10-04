"use strict";

(() => {
  const form = document.querySelector("#report-form");
  const input = document.querySelector("#media-input");
  const drop = document.querySelector("#drop-area");
  const list = document.querySelector("#media-list");
  const total = document.querySelector("#media-total");
  const status = document.querySelector("#status");
  const send = document.querySelector("#submit");
  const receipt = document.querySelector("#receipt");
  const receiptFields = document.querySelector("#receipt-fields");
  const another = document.querySelector("#another");
  const FILE_LIMIT = 100 * 1024 * 1024;
  const ISSUE_LIMIT = 250 * 1024 * 1024;
  const ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  const messages = {
    PHOTO_METADATA_UNSTRIPPED: "This photo could not be made private. Try another photo.",
    MEDIA_STAGE_UNAVAILABLE: "This server could not safely prepare that video. Try a different video or send the team a note.",
    CONFLICT: "That file conflicts with an earlier upload. Choose it again and retry.",
    BOARD_BUSY: "The team’s issue board is busy. Please wait a moment and retry.",
    ISSUES_DISABLED: "This team is not accepting issue reports right now.",
    RATE_LIMITED: "Too many uploads or reports right now. Please wait a moment and retry.",
    PAYLOAD_TOO_LARGE: "That file or report is too large. Remove a file or choose a smaller one.",
    MEDIA_QUOTA_EXCEEDED: "This report has reached its media limit. Remove a file or try again later.",
    FORBIDDEN: "This report link cannot be used from this page.",
  };
  let files = [];
  let sourceRef = null;
  let opId = null;

  function randomUlid() {
    let time = Date.now();
    let head = "";
    for (let i = 0; i < 10; i += 1) {
      head = ALPHABET[time % 32] + head;
      time = Math.floor(time / 32);
    }
    const bytes = crypto.getRandomValues(new Uint8Array(10));
    let bits = 0;
    let value = 0;
    let tail = "";
    for (const byte of bytes) {
      value = (value << 8) | byte;
      bits += 8;
      while (bits >= 5) {
        bits -= 5;
        tail += ALPHABET[(value >>> bits) & 31];
      }
    }
    return head + tail.slice(0, 16);
  }

  function ensureIds() {
    if (!sourceRef) sourceRef = randomUlid();
    if (!opId) opId = `op_${randomUlid()}`;
  }

  function humanSize(bytes) {
    if (bytes === 0) return "0 B";
    if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  function stagedSize(item) {
    let bytes = item?.payload?.size || 0;
    for (const frame of item?.frames || []) bytes += frame.payload?.size || 0;
    return bytes;
  }

  function show(message, isError = false) {
    status.textContent = message;
    status.classList.toggle("error", isError);
  }

  function renderFiles() {
    list.replaceChildren();
    let bytes = 0;
    for (const entry of files) {
      bytes += entry.staged ? stagedSize(entry.staged) : entry.file.size;
      const row = document.createElement("li");
      row.className = "media-row";
      const label = document.createElement("span");
      label.className = "media-name";
      label.textContent = `${entry.file.name} · ${humanSize(entry.file.size)}`;
      const remove = document.createElement("button");
      remove.className = "remove-media";
      remove.type = "button";
      remove.textContent = "Remove";
      remove.addEventListener("click", () => {
        files = files.filter((item) => item !== entry);
        renderFiles();
      });
      row.append(label, remove);
      list.append(row);
    }
    total.textContent = `${files.length} ${files.length === 1 ? "file" : "files"} · ${humanSize(bytes)}`;
  }

  function addFiles(incoming) {
    const accepted = [...incoming].filter((file) => file.type.startsWith("image/") || file.type.startsWith("video/"));
    const existing = new Set(files.map((entry) => `${entry.file.name}:${entry.file.size}:${entry.file.lastModified}`));
    const additions = accepted.filter((file) => !existing.has(`${file.name}:${file.size}:${file.lastModified}`));
    if (additions.some((file) => file.size > FILE_LIMIT)) {
      show("Each photo or video must be 100 MB or smaller.", true);
      return;
    }
    if (additions.reduce((n, file) => n + file.size, files.reduce((n, entry) => n + (entry.staged ? stagedSize(entry.staged) : entry.file.size), 0)) > ISSUE_LIMIT) {
      show("Together, files must fit within 250 MB. Remove a file or choose a smaller one.", true);
      return;
    }
    files.push(...additions.map((file) => ({ file, staged: null })));
    renderFiles();
    if (additions.length) show("Files are ready to upload when you send the report.");
  }

  function messageFor(code) {
    return messages[code] || "We couldn’t send this report. Check the form and try again.";
  }

  async function responseData(response) {
    let body;
    try { body = await response.json(); } catch { throw new Error(messageFor("GENERIC")); }
    if (!response.ok || body?.ok === false) {
      const code = body?.error?.code || "GENERIC";
      throw Object.assign(new Error(messageFor(code)), { code, retryAfter: response.headers.get("Retry-After") });
    }
    if (body && ["id", "short_id", "filed_at", "source", "source_ref", "external", "deduplicated"]
      .every((key) => Object.prototype.hasOwnProperty.call(body, key))) return body;
    return body.data;
  }

  async function upload(entry) {
    ensureIds();
    show(`Preparing ${entry.file.name}…`);
    const digest = [...new Uint8Array(await crypto.subtle.digest("SHA-256", await entry.file.arrayBuffer()))]
      .map((byte) => byte.toString(16).padStart(2, "0")).join("");
    const url = `media/${sourceRef}/${digest}?filename=${encodeURIComponent(entry.file.name)}`;
    const response = await fetch(url, {
      method: "PUT",
      headers: { "Content-Type": "application/octet-stream" },
      body: entry.file,
      credentials: "omit",
      cache: "no-store",
    });
    entry.staged = await responseData(response);
    renderFiles();
  }

  function renderReceipt(data) {
    const labels = [
      ["id", "Receipt ID"],
      ["short_id", "Short ID"],
      ["filed_at", "Filed at"],
      ["source", "Source"],
      ["source_ref", "Reference"],
      ["external", "External report"],
      ["deduplicated", "Retry matched"],
    ];
    receiptFields.replaceChildren();
    for (const [key, labelText] of labels) {
      const dt = document.createElement("dt");
      const dd = document.createElement("dd");
      dt.textContent = labelText;
      dd.textContent = typeof data[key] === "boolean" ? (data[key] ? "Yes" : "No") : String(data[key] ?? "");
      receiptFields.append(dt, dd);
    }
    form.hidden = true;
    receipt.hidden = false;
    show("");
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const title = document.querySelector("#title").value.trim();
    if (!title) {
      show("Add a title before sending.", true);
      document.querySelector("#title").focus();
      return;
    }
    ensureIds();
    send.disabled = true;
    try {
      for (const entry of files) if (!entry.staged) await upload(entry);
      show("Sending the report…");
      const response = await fetch("submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "omit",
        cache: "no-store",
        body: JSON.stringify({
          op_id: opId,
          source_ref: sourceRef,
          title,
          description: document.querySelector("#description").value,
          reporter_name: document.querySelector("#reporter-name").value,
          reporter_email: document.querySelector("#reporter-email").value,
          media: files.map((entry) => entry.staged).filter(Boolean),
        }),
      });
      renderReceipt(await responseData(response));
    } catch (error) {
      show(error.message || messageFor("GENERIC"), true);
      if (error.code && !["RATE_LIMITED", "BOARD_BUSY"].includes(error.code)) {
        for (const entry of files) entry.staged = null;
        renderFiles();
      }
    } finally {
      send.disabled = false;
    }
  });

  drop.addEventListener("click", () => input.click());
  input.addEventListener("change", () => { addFiles(input.files); input.value = ""; });
  for (const eventName of ["dragenter", "dragover"]) {
    drop.addEventListener(eventName, (event) => { event.preventDefault(); drop.classList.add("dragging"); });
  }
  for (const eventName of ["dragleave", "drop"]) {
    drop.addEventListener(eventName, (event) => { event.preventDefault(); drop.classList.remove("dragging"); });
  }
  drop.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
  document.addEventListener("paste", (event) => {
    const pasted = [...(event.clipboardData?.items || [])].filter((item) => item.kind === "file").map((item) => item.getAsFile()).filter(Boolean);
    if (pasted.length) addFiles(pasted);
  });
  another.addEventListener("click", () => {
    files = [];
    sourceRef = null;
    opId = null;
    form.reset();
    form.hidden = false;
    receipt.hidden = true;
    renderFiles();
  });
  renderFiles();
})();
