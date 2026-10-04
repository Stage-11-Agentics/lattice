"use strict";

// Exercises the shipped reporter form script with the project's MiniDOM. The
// live-server tests cover the direct route; these two page URLs pin relative
// resource and write resolution both at /r/ and behind a stripped path prefix.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { webcrypto } = require("node:crypto");
const { createWindow, Event } = require("./support/mini-dom.js");

const STATIC = path.join(__dirname, "..", "..", "src", "lattice", "dashboard", "static", "reporter");
const HTML = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
const SCRIPT = fs.readFileSync(path.join(STATIC, "reporter.js"), "utf8");

const RECEIPT = {
  id: "iss_01ABCDEF",
  short_id: "ALP-I4",
  filed_at: "2026-10-04T18:00:00Z",
  source: "reporter-link:link_01ABCDEF",
  source_ref: "01J9MOBILEFORMTEST000000000",
  external: true,
  deduplicated: false,
};

function addReporterDomMethods(document) {
  const prototype = Object.getPrototypeOf(document.body);
  prototype.replaceChildren = function (...nodes) {
    this.childNodes.slice().forEach((node) => this.removeChild(node));
    nodes.forEach((node) => this.appendChild(node));
  };
  prototype.append = function (...nodes) { nodes.forEach((node) => this.appendChild(node)); };
}

async function renderForm(pageURL) {
  const win = createWindow();
  const doc = win.document;
  addReporterDomMethods(doc);
  doc.body.innerHTML = HTML;

  const calls = [];
  const fetchStub = async (url, options) => {
    calls.push({ url, options });
    const body = options.method === "PUT"
      ? { ok: true, data: { payload: { sha256: "a".repeat(64), bytes: 3, media_type: "image/png" } } }
      : { ok: true, data: { ...RECEIPT, title: "Private issue title", description: "Private issue description" } };
    return { ok: true, status: options.method === "PUT" ? 201 : 200, json: async () => body, headers: { get: () => null } };
  };

  vm.runInNewContext(SCRIPT, { document: doc, crypto: webcrypto, fetch: fetchStub }, { filename: "reporter.js" });
  const input = doc.querySelector("#media-input");
  input.files = [{
    name: "photo.png",
    type: "image/png",
    size: 3,
    lastModified: 1,
    arrayBuffer: async () => new Uint8Array([1, 2, 3]).buffer,
  }];
  input.dispatchEvent(new Event("change"));
  doc.querySelector("#title").value = "Private issue title";
  doc.querySelector("#description").value = "Private issue description";

  const form = doc.querySelector("#report-form");
  const receipt = doc.querySelector("#receipt");
  form.dispatchEvent(new Event("submit", { cancelable: true }));
  const deadline = Date.now() + 2000;
  while (receipt.hidden !== false && Date.now() < deadline) {
    await new Promise((resolve) => setImmediate(resolve));
  }

  try {
    assert.equal(receipt.hidden, false, "successful submission shows the receipt");
    assert.equal(form.hidden, true, "successful submission hides the form");
    assert.equal(calls.length, 2, "one media upload and one issue submission are sent");
    assert.equal(calls[0].options.method, "PUT");
    assert.equal(calls[1].options.method, "POST");
    assert.equal(calls[0].options.credentials, "omit");
    assert.equal(calls[1].options.credentials, "omit");

    const requests = calls.map((call) => new URL(call.url, pageURL));
    assert.ok(requests.every((url) => url.origin === new URL(pageURL).origin));
    assert.ok(requests.every((url) => url.pathname.startsWith(new URL(pageURL).pathname)));
    assert.match(requests[0].pathname, /\/media\/[0-9A-HJKMNP-TV-Z]{26}\/[a-f0-9]{64}$/);
    assert.equal(requests[1].pathname, new URL("submit", pageURL).pathname);
    assert.equal(new URL(calls[0].url, pageURL).pathname, requests[0].pathname);
    assert.equal(calls[0].url.startsWith("/"), false, "upload path stays relative");
    assert.equal(calls[1].url.startsWith("/"), false, "submit path stays relative");

    const fields = receipt.querySelectorAll("dt");
    const values = receipt.querySelectorAll("dd");
    assert.equal(fields.length, 7);
    assert.equal(values.length, 7);
    assert.deepEqual(fields.map((field) => field.textContent), [
      "Receipt ID",
      "Short ID",
      "Filed at",
      "Source",
      "Reference",
      "External report",
      "Retry matched",
    ]);
    assert.deepEqual(values.map((field) => field.textContent), [
      RECEIPT.id,
      RECEIPT.short_id,
      RECEIPT.filed_at,
      RECEIPT.source,
      RECEIPT.source_ref,
      "Yes",
      "No",
    ]);
    assert.equal(receipt.textContent.includes("Private issue title"), false);
    assert.equal(receipt.textContent.includes("Private issue description"), false);
    assert.equal(receipt.textContent.includes("title"), false, "no issue view fields are rendered");

    const cssRef = doc.querySelector("link[rel=stylesheet]").getAttribute("href");
    const scriptRef = doc.querySelector("script[defer]").getAttribute("src");
    assert.equal(cssRef, "./reporter.css");
    assert.equal(scriptRef, "./reporter.js");
    assert.equal(new URL(cssRef, pageURL).pathname, new URL("./reporter.css", pageURL).pathname);
    assert.equal(new URL(scriptRef, pageURL).pathname, new URL("./reporter.js", pageURL).pathname);
  } finally {
    // The form script ran in an isolated VM context, so it leaves no global state.
  }
}

test("reporter assets and form requests stay relative on direct and prefix routes", async (t) => {
  assert.match(HTML, /<meta name="viewport" content="width=device-width, initial-scale=1">/);
  const css = fs.readFileSync(path.join(STATIC, "reporter.css"), "utf8");
  assert.match(css, /@media \(max-width: 390px\)/, "375px screens use the compact layout");

  await t.test("direct /r/ route", async () => {
    await renderForm("http://report.example.test/r/rpt_testsecret/");
  });
  await t.test("prefix-stripping proxy route", async () => {
    await renderForm("http://report.example.test/prefix/reports/r/rpt_testsecret/");
  });
});

test("an edited resend uses a new op_id and keeps its source_ref after a lost response", async () => {
  const win = createWindow();
  const doc = win.document;
  addReporterDomMethods(doc);
  doc.body.innerHTML = HTML;
  const attempts = [];
  const fetchStub = async (_url, options) => {
    const body = JSON.parse(options.body);
    attempts.push(body);
    if (attempts.length === 1) throw new TypeError("The network response was lost");
    return {
      ok: true,
      status: 200,
      json: async () => ({ ok: true, data: { ...RECEIPT, deduplicated: true } }),
      headers: { get: () => null },
    };
  };
  vm.runInNewContext(SCRIPT, { document: doc, crypto: webcrypto, fetch: fetchStub });

  const form = doc.querySelector("#report-form");
  const receipt = doc.querySelector("#receipt");
  doc.querySelector("#title").value = "First attempt";
  form.dispatchEvent(new Event("submit", { cancelable: true }));
  const failedDeadline = Date.now() + 2000;
  while (!doc.querySelector("#status").classList.contains("error") && Date.now() < failedDeadline) {
    await new Promise((resolve) => setImmediate(resolve));
  }

  doc.querySelector("#title").value = "Edited resend";
  form.dispatchEvent(new Event("submit", { cancelable: true }));
  const receiptDeadline = Date.now() + 2000;
  while (receipt.hidden !== false && Date.now() < receiptDeadline) {
    await new Promise((resolve) => setImmediate(resolve));
  }

  assert.equal(attempts.length, 2);
  assert.notEqual(attempts[0].op_id, attempts[1].op_id);
  assert.equal(attempts[0].source_ref, attempts[1].source_ref);
  assert.equal(attempts[0].title, "First attempt");
  assert.equal(attempts[1].title, "Edited resend");
  assert.equal(receipt.hidden, false);
});

test("unsupported and empty-MIME files produce a visible skipped count", () => {
  const win = createWindow();
  const doc = win.document;
  addReporterDomMethods(doc);
  doc.body.innerHTML = HTML;
  vm.runInNewContext(SCRIPT, { document: doc, crypto: webcrypto, fetch: async () => { throw new Error("unexpected upload"); } });

  const input = doc.querySelector("#media-input");
  input.files = [
    { name: "map.gif", type: "image/gif", size: 12, lastModified: 1 },
    { name: "unknown.bin", type: "", size: 13, lastModified: 2 },
    { name: "photo.png", type: "image/png", size: 14, lastModified: 3 },
  ];
  input.dispatchEvent(new Event("change"));

  assert.match(doc.querySelector("#status").textContent, /2 files skipped:/);
  assert.match(doc.querySelector("#status").textContent, /WebP and GIF photos are refused by the privacy rule/);
  assert.match(doc.querySelector("#status").textContent, /no recognizable file type/);
  assert.equal(doc.querySelector("#media-total").textContent.startsWith("1 file"), true);
  assert.match(HTML, /Photos: JPEG, PNG or HEIC\. Videos are supported\./);
  assert.match(HTML, /WebP and GIF photos are refused/);
});
