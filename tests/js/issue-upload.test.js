"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { webcrypto } = require("node:crypto");
const { uploadIssueMedia } = require("../../src/lattice/dashboard/static/issue-upload.js");

function file() {
  const bytes = new Uint8Array([137, 80, 78, 71, 1]);
  return {
    name: "Screenshot 1.png",
    arrayBuffer: async () => bytes.buffer,
  };
}

test("non-JSON upload failures show a readable status instead of a JSON parser error", async (t) => {
  for (const status of [413, 502]) {
    await t.test("HTTP " + status, async () => {
      const error = await uploadIssueMedia(file(), {
        crypto: webcrypto,
        fetch: async () => ({ status, json: async () => { throw new SyntaxError("Unexpected token < in JSON"); } }),
        url: (path) => "/p/alpha/" + path,
      }).then(() => null, (failure) => failure);

      assert.equal(error.message, "Media upload failed (HTTP " + status + "). The server returned an unreadable response.");
      assert.doesNotMatch(error.message, /Unexpected token|<html/i);
    });
  }
});

test("JSON upload refusals retain their server message and code", async () => {
  await assert.rejects(
    uploadIssueMedia(file(), {
      crypto: webcrypto,
      fetch: async () => ({
        status: 413,
        json: async () => ({ ok: false, error: { code: "PAYLOAD_TOO_LARGE", message: "File exceeds the limit." } }),
      }),
      url: (path) => path,
    }),
    (error) => error.message === "File exceeds the limit." && error.code === "PAYLOAD_TOO_LARGE",
  );
});

test("successful uploads return the staged payload", async () => {
  let request;
  const result = await uploadIssueMedia(file(), {
    crypto: webcrypto,
    fetch: async (url, options) => {
      request = { url, options };
      return { status: 201, json: async () => ({ ok: true, data: { payload: { staged: true } } }) };
    },
    url: (path) => "/p/alpha/" + path,
  });

  assert.deepEqual(result, { payload: { staged: true } });
  assert.match(request.url, /^\/p\/alpha\/issues\/media\/staging\/[a-f0-9]{64}\?filename=Screenshot%201\.png$/);
  assert.equal(request.options.method, "PUT");
  assert.equal(request.options.headers["Content-Type"], "application/octet-stream");
});
