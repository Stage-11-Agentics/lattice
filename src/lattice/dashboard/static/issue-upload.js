/* Hosted issue media upload: keep proxy HTML/error pages readable to the filer. */
(function (root) {
  "use strict";

  async function uploadIssueMedia(file, options) {
    var bytes = await file.arrayBuffer();
    var digest = await options.crypto.subtle.digest("SHA-256", bytes);
    var sha256 = Array.prototype.map.call(new Uint8Array(digest), function (byte) {
      return byte.toString(16).padStart(2, "0");
    }).join("");
    var filename = encodeURIComponent(file.name || "attachment");
    var response = await options.fetch(options.url(
      "issues/media/staging/" + sha256 + "?filename=" + filename
    ), {
      method: "PUT",
      credentials: "same-origin",
      headers: { "Content-Type": "application/octet-stream" },
      body: file
    });
    var envelope;
    try {
      envelope = await response.json();
    } catch (_error) {
      throw new Error("Media upload failed (HTTP " + response.status + "). The server returned an unreadable response.");
    }
    if (!envelope || envelope.ok !== true) {
      var error = new Error(envelope && envelope.error && envelope.error.message
        ? envelope.error.message
        : "Media upload failed (HTTP " + response.status + ").");
      error.code = envelope && envelope.error ? envelope.error.code || null : null;
      throw error;
    }
    return envelope.data;
  }

  root.IssueUpload = { uploadIssueMedia: uploadIssueMedia };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.IssueUpload;
  }
})(typeof window !== "undefined" ? window : globalThis);
