// Client presentation layer for the 3D Printer Debugger (web.md §2.1).
//
// Hand-written, no framework, covering exactly the four browser-only capabilities: camera capture,
// audio recording, upload progress, and consuming the SSE stream — plus the approval interface and
// the emergency stop, which are driven by the stream. Everything else is server-rendered HTML.

"use strict";

(function () {
  const AUDIO_CAP_MS = 120000; // Two-minute default cap (web.md §7).

  document.addEventListener("DOMContentLoaded", () => {
    wireNewSession();
    wirePrinterImport();
    const view = document.querySelector(".session-view");
    if (view) {
      const sessionId = view.dataset.sessionId;
      wireComposer(sessionId);
      wireRename(sessionId);
      wireCamera(sessionId);
      wireFileAttach(sessionId);
      wireMic(sessionId);
      wireEstop();
      connectStream(view.dataset.stream);
    }
  });

  // --- Session list: create a session and navigate to it -------------------------------------
  function wireNewSession() {
    const button = document.getElementById("new-session");
    if (!button) return;
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const res = await fetch("/sessions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: "New session" }),
        });
        const data = await res.json();
        if (data.id) window.location.href = "/sessions/" + encodeURIComponent(data.id);
      } finally {
        button.disabled = false;
      }
    });
  }

  // --- Printer import: upload a knowledge-base .md and show the ingest outcome ---------------
  function wirePrinterImport() {
    const input = document.getElementById("printer-import-input");
    const results = document.getElementById("import-results");
    if (!input) return;
    input.addEventListener("change", async () => {
      const file = input.files && input.files[0];
      if (!file) return;
      if (results) {
        results.hidden = false;
        results.textContent = "Importing " + (file.name || "document") + "…";
      }
      try {
        const res = await fetch("/printers/import", {
          method: "POST",
          headers: { "Content-Type": "text/markdown", "X-Filename": file.name || "" },
          body: file,
        });
        const data = await res.json().catch(() => ({}));
        renderImportResults(results, res, data);
      } catch (err) {
        if (results) {
          results.hidden = false;
          results.textContent = "Import failed: " + err;
        }
      }
      input.value = "";
    });
  }

  function renderImportResults(results, res, data) {
    if (!results) return;
    results.hidden = false;
    results.innerHTML = "";
    if (!res.ok) {
      const p = document.createElement("p");
      p.className = "import-error";
      p.textContent = data && data.error ? data.error : "Import failed (" + res.status + ").";
      results.appendChild(p);
      return;
    }
    const upserted = data.printers_upserted || [];
    const degraded = data.printers_degraded || [];
    const summary = document.createElement("p");
    summary.className = "import-summary";
    let text = upserted.length + (upserted.length === 1 ? " printer" : " printers") + " imported";
    if (degraded.length) text += ", " + degraded.length + " with degraded config";
    summary.textContent = text + ".";
    results.appendChild(summary);
    // Surface the ingester's user-facing notes (degraded reasons, missing config paths, etc.).
    const messages = data.messages || [];
    if (messages.length) {
      const ul = document.createElement("ul");
      ul.className = "import-messages";
      messages.forEach((m) => {
        const li = document.createElement("li");
        li.textContent = m;
        ul.appendChild(li);
      });
      results.appendChild(ul);
    }
  }

  // --- Composer: optimistic user message + POST ----------------------------------------------
  function wireComposer(sessionId) {
    const form = document.getElementById("composer");
    const text = document.getElementById("composer-text");
    if (!form || !text) return;
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const value = text.value.trim();
      if (!value) return;
      appendMessage("user", value); // Optimistic: the user's message appears immediately.
      text.value = "";
      const content = [{ type: "text", text: value }];
      await fetch("/sessions/" + encodeURIComponent(sessionId) + "/messages", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: content }),
      });
    });
  }

  // --- Rename: inline-edit the session title, saving on Enter/blur ----------------------------
  function wireRename(sessionId) {
    const button = document.getElementById("rename-btn");
    const title = document.querySelector(".session-title");
    if (!button || !title) return;
    button.addEventListener("click", () => startRename(sessionId, title));
  }

  function startRename(sessionId, title) {
    if (title.querySelector("input")) return; // Already editing.
    const current = title.textContent;
    const input = document.createElement("input");
    input.type = "text";
    input.className = "rename-input";
    input.value = current;
    title.textContent = "";
    title.appendChild(input);
    input.focus();
    input.select();

    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      const next = input.value.trim();
      if (!save || !next || next === current) {
        title.textContent = current; // Cancel or no change: restore the original.
        return;
      }
      try {
        const res = await fetch("/sessions/" + encodeURIComponent(sessionId) + "/rename", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: next }),
        });
        if (res.ok) {
          title.textContent = next;
          document.title = next + " — 3D Printer Debugger";
        } else {
          title.textContent = current;
          appendMessage("system", "Rename failed (" + res.status + ").");
        }
      } catch (err) {
        title.textContent = current;
        appendMessage("system", "Rename failed: " + err);
      }
    };

    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        finish(true);
      } else if (e.key === "Escape") {
        e.preventDefault();
        finish(false);
      }
    });
    input.addEventListener("blur", () => finish(true));
  }

  // --- Camera capture: file input with capture=environment, preview + upload -----------------
  function wireCamera(sessionId) {
    const input = document.getElementById("camera-input");
    if (!input) return;
    input.addEventListener("change", async () => {
      const file = input.files && input.files[0];
      if (!file) return;
      previewImage(file);
      const upload = await prepareImageForUpload(file);
      // On a successful upload the photo enters the conversation as a user message and fires a
      // turn — the agent reacts and its reply streams back over SSE (web.md §7).
      uploadWithProgress(sessionId, upload, "photo", (artifactId) =>
        sendImageMessage(sessionId, artifactId, upload.type || "image/jpeg")
      );
      input.value = "";
    });
  }

  function wireFileAttach(sessionId) {
    const input = document.getElementById("file-input");
    if (!input) return;
    input.addEventListener("change", async () => {
      const file = input.files && input.files[0];
      if (!file) return;
      // Only images are re-encoded; .gcode/.3mf files upload byte-for-byte.
      const isImage = file.type && file.type.startsWith("image/");
      const upload = isImage ? await prepareImageForUpload(file) : file;
      // Only an image fires a turn; a .gcode/.3mf upload is stored/indexed with no message.
      const onUploaded = isImage
        ? (artifactId) => sendImageMessage(sessionId, artifactId, upload.type || "image/jpeg")
        : null;
      uploadWithProgress(sessionId, upload, "file", onUploaded);
      input.value = "";
    });
  }

  // Post the uploaded photo as a lean image reference message, rendering it optimistically in the
  // conversation thread first so it appears without a refresh. The reference points at the stored
  // artifact; the agent's streamed reply arrives over the existing SSE path.
  function sendImageMessage(sessionId, artifactId, mediaType) {
    appendImageMessage(artifactId);
    const content = [{ type: "image", artifact_id: artifactId, media_type: mediaType }];
    fetch("/sessions/" + encodeURIComponent(sessionId) + "/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: content }),
    });
  }

  // --- Image downscaling: bound the longest edge to 2048px and re-encode as JPEG q0.9 ---------
  const MAX_EDGE = 2048;
  const JPEG_QUALITY = 0.9;

  async function prepareImageForUpload(file) {
    if (!file.type || !file.type.startsWith("image/")) return file;
    try {
      const blob = await downscaleImage(file);
      if (!blob) return file;
      const name = jpgName(file.name);
      try {
        return new File([blob], name, { type: "image/jpeg" });
      } catch (e) {
        blob.name = name; // Some browsers lack the File constructor; tag the Blob instead.
        return blob;
      }
    } catch (err) {
      // A format the browser cannot decode: fall back to the original file (part b reports any
      // resulting upload failure). Log so the silent-failure case leaves a trace.
      console.error("Image downscale failed; uploading the original file.", err);
      return file;
    }
  }

  function downscaleImage(file) {
    return new Promise((resolve, reject) => {
      const render = (width, height, paint) => {
        const scale = Math.min(1, MAX_EDGE / Math.max(width, height));
        const w = Math.max(1, Math.round(width * scale));
        const h = Math.max(1, Math.round(height * scale));
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        const ctx = canvas.getContext("2d");
        if (!ctx) {
          reject(new Error("no 2d canvas context"));
          return;
        }
        paint(ctx, w, h);
        canvas.toBlob(
          (blob) => (blob ? resolve(blob) : reject(new Error("toBlob returned null"))),
          "image/jpeg",
          JPEG_QUALITY
        );
      };
      if (window.createImageBitmap) {
        createImageBitmap(file).then((bitmap) => {
          render(bitmap.width, bitmap.height, (ctx, w, h) => {
            ctx.drawImage(bitmap, 0, 0, w, h);
            if (bitmap.close) bitmap.close();
          });
        }, reject);
      } else {
        const url = URL.createObjectURL(file);
        const img = new Image();
        img.onload = () => {
          try {
            render(img.naturalWidth || img.width, img.naturalHeight || img.height, (ctx, w, h) => {
              ctx.drawImage(img, 0, 0, w, h);
            });
          } finally {
            URL.revokeObjectURL(url);
          }
        };
        img.onerror = () => {
          URL.revokeObjectURL(url);
          reject(new Error("image decode failed"));
        };
        img.src = url;
      }
    });
  }

  function jpgName(name) {
    if (!name) return "photo.jpg";
    return name.replace(/\.[^.]+$/, "") + ".jpg";
  }

  function previewImage(file) {
    const url = URL.createObjectURL(file);
    const fig = document.createElement("figure");
    fig.className = "attachment attachment-image";
    const img = document.createElement("img");
    img.src = url;
    img.alt = "captured photo";
    img.onload = () => URL.revokeObjectURL(url);
    fig.appendChild(img);
    const cap = document.createElement("figcaption");
    cap.textContent = file.name || "photo";
    fig.appendChild(cap);
    const attachments = document.getElementById("attachments");
    if (attachments) attachments.appendChild(fig);
  }

  // --- Upload progress: XHR so large .3mf/.gcode uploads show a bar (web.md §6) ---------------
  function uploadWithProgress(sessionId, file, kind, onUploaded) {
    const wrap = document.getElementById("upload-progress");
    const bar = document.getElementById("upload-bar");
    const label = document.getElementById("upload-label");
    if (wrap) wrap.hidden = false;
    if (label) label.textContent = "Uploading " + (file.name || kind) + "…";

    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/sessions/" + encodeURIComponent(sessionId) + "/files");
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");
    xhr.setRequestHeader("X-Filename", file.name || "");
    xhr.upload.addEventListener("progress", (event) => {
      if (event.lengthComputable && bar) bar.value = (event.loaded / event.total) * 100;
    });
    xhr.addEventListener("load", () => {
      if (xhr.status >= 400) {
        if (label) {
          label.textContent =
            xhr.status === 413
              ? "File too large — rejected before upload finished."
              : "Upload failed (" + xhr.status + ").";
        }
        // A tiny label is not enough: post a visible system message with the reason.
        appendMessage("system", uploadFailureText(file, kind, xhr.status, failureReason(xhr)));
      } else {
        if (label) label.textContent = "Uploaded " + (file.name || kind) + ".";
        if (bar) bar.value = 100;
        if (onUploaded) {
          let artifactId = "";
          try {
            artifactId = (JSON.parse(xhr.responseText) || {}).artifact_id || "";
          } catch (e) {}
          if (artifactId) onUploaded(artifactId);
        }
      }
      hideLater(wrap);
    });
    xhr.addEventListener("error", () => {
      if (label) label.textContent = "Upload failed.";
      appendMessage("system", uploadFailureText(file, kind, 0, "the connection dropped"));
      hideLater(wrap);
    });
    xhr.send(file);
  }

  function failureReason(xhr) {
    try {
      const data = JSON.parse(xhr.responseText);
      if (data && data.error) return data.error;
    } catch (e) {
      // Not a JSON body; fall through to the raw text.
    }
    return xhr.responseText || "";
  }

  function uploadFailureText(file, kind, status, reason) {
    const isImage = kind === "photo" || (file.type && file.type.startsWith("image/"));
    const what = isImage ? "photo" : "file";
    let msg = "The " + what + " " + (file.name ? '"' + file.name + '" ' : "") + "failed to upload";
    if (status) msg += " (status " + status + ")";
    if (reason) msg += ": " + reason;
    return msg + ".";
  }

  function hideLater(wrap) {
    if (wrap) setTimeout(() => (wrap.hidden = true), 2500);
  }

  // --- Audio recording: MediaRecorder, cap-and-submit, upload as an artifact (web.md §7) ------
  let mediaRecorder = null;
  let audioChunks = [];
  let audioCapTimer = null;

  function disableMic(button, message) {
    // The button stays clickable so the reason is explained on tap, but reads as disabled.
    button.classList.add("disabled");
    button.setAttribute("aria-disabled", "true");
    button.addEventListener("click", () => appendMessage("system", message));
  }

  function wireMic(sessionId) {
    const button = document.getElementById("mic-btn");
    if (!button) return;
    // getUserMedia/MediaRecorder are exposed only in a secure context (HTTPS or localhost). On a
    // plain http:// LAN origin navigator.mediaDevices is undefined, so explain rather than die.
    const media = navigator.mediaDevices;
    if (!window.isSecureContext || !media || !media.getUserMedia) {
      disableMic(
        button,
        "Voice recording needs a secure (HTTPS) connection. You're on an insecure http:// " +
          "origin, so the browser blocked the microphone. Reload the page over https:// " +
          "(accept the one-time certificate warning) to record."
      );
      return;
    }
    if (!window.MediaRecorder) {
      disableMic(button, "This browser doesn't support audio recording (MediaRecorder).");
      return;
    }
    button.addEventListener("click", async () => {
      if (mediaRecorder && mediaRecorder.state === "recording") {
        stopRecording();
        return;
      }
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        audioChunks = [];
        mediaRecorder = new MediaRecorder(stream);
        mediaRecorder.addEventListener("dataavailable", (e) => {
          if (e.data && e.data.size) audioChunks.push(e.data);
        });
        mediaRecorder.addEventListener("stop", () => {
          stream.getTracks().forEach((t) => t.stop());
          submitAudio(sessionId);
        });
        mediaRecorder.start();
        document.body.classList.add("recording");
        // On reaching the cap, recording stops and what was captured is submitted (web.md §7).
        audioCapTimer = setTimeout(() => {
          appendMessage("system", "Recording reached the 2-minute cap and was submitted.");
          stopRecording();
        }, AUDIO_CAP_MS);
      } catch (err) {
        appendMessage("system", "Microphone unavailable: " + err);
      }
    });
  }

  function stopRecording() {
    if (audioCapTimer) clearTimeout(audioCapTimer);
    audioCapTimer = null;
    document.body.classList.remove("recording");
    if (mediaRecorder && mediaRecorder.state !== "inactive") mediaRecorder.stop();
  }

  function submitAudio(sessionId) {
    if (!audioChunks.length) return;
    const blob = new Blob(audioChunks, { type: "audio/webm" });
    // Transcription (Whisper) runs server-side; the server also feeds the transcript to the
    // session so the agent answers it. We just surface the returned text (or the status).
    appendMessage("system", "Audio captured — transcribing…");
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/sessions/" + encodeURIComponent(sessionId) + "/audio");
    xhr.setRequestHeader("Content-Type", "audio/webm");
    xhr.onload = () => {
      let text = "";
      try { text = (JSON.parse(xhr.responseText) || {}).transcription || ""; } catch (e) {}
      if (text && text !== "pending" && text !== "failed") {
        appendMessage("user", text);
      } else if (text === "failed") {
        appendMessage("system", "Transcription failed — the clip was saved.");
      } else {
        appendMessage("system", "Transcription pending — the clip was saved.");
      }
    };
    xhr.send(blob);
  }

  // --- SSE consumer: EventSource with position-based reconnect (web.md §10) -------------------
  let lastEventId = 0;
  let source = null;

  function connectStream(streamUrl) {
    if (!streamUrl) return;
    const url = lastEventId ? streamUrl + "?last_id=" + lastEventId : streamUrl;
    source = new EventSource(url);
    source.addEventListener("message", onStreamEvent);
    source.addEventListener("assistant", onStreamEvent);
    source.addEventListener("tool", onToolEvent);
    source.addEventListener("proposal", onProposalEvent);
    source.addEventListener("approval_resolved", onApprovalResolved);
    source.addEventListener("printer", onPrinterEvent);
    source.addEventListener("agent_error", onAgentErrorEvent);
    source.addEventListener("error", () => {
      // Reconnect from the last position so backgrounding does not lose output (web.md §10).
      if (source) source.close();
      setTimeout(() => connectStream(streamUrl), 2000);
    });
  }

  function trackId(event) {
    const id = parseInt(event.lastEventId, 10);
    if (!isNaN(id) && id > lastEventId) lastEventId = id;
  }

  function onStreamEvent(event) {
    trackId(event);
    const data = parseData(event.data);
    if (data && (data.text || data.content)) appendMessage("assistant", data.text || data.content);
  }

  // A failed agent turn arrives on its own channel (never the reserved "error" event) and is
  // shown as a visible system message so a failure is no longer silent.
  function onAgentErrorEvent(event) {
    trackId(event);
    const data = parseData(event.data);
    const message = data && data.message ? data.message : "The agent turn failed.";
    appendMessage("system", "⚠️ " + message);
  }

  function onToolEvent(event) {
    trackId(event);
    const data = parseData(event.data);
    const el = document.createElement("div");
    el.className = "message role-assistant tool-activity";
    el.textContent = data && data.tool ? "running " + data.tool + "…" : "working…";
    appendNode(el);
  }

  function onPrinterEvent(event) {
    trackId(event);
    const data = parseData(event.data);
    const strip = document.querySelector(".printer-strip");
    if (!strip || !data) return;
    strip.classList.remove("stale");
    setField(strip, "connection", data.connection);
    setField(strip, "nozzle", data.nozzle != null ? "nozzle " + data.nozzle + "°C" : null);
    setField(strip, "bed", data.bed != null ? "bed " + data.bed + "°C" : null);
    setField(strip, "print", data.print);
  }

  function setField(root, name, value) {
    if (value == null) return;
    const el = root.querySelector('[data-field="' + name + '"]');
    if (el) el.textContent = value;
  }

  // --- Approval interface (web.md §5): verbatim command, danger flags, countdown -------------
  function onProposalEvent(event) {
    trackId(event);
    const data = parseData(event.data);
    if (data) renderApproval(data);
  }

  function onApprovalResolved(event) {
    trackId(event);
    const data = parseData(event.data);
    // Resolving on one viewer resolves it on all (web.md §5): clear the block for this one too.
    const slot = document.getElementById("approval-slot");
    if (!slot) return;
    const block = data && data.tool_call_id
      ? slot.querySelector('[data-tool-call-id="' + cssEscape(data.tool_call_id) + '"]')
      : slot.firstElementChild;
    if (block) block.remove();
  }

  function renderApproval(data) {
    const slot = document.getElementById("approval-slot");
    const template = document.getElementById("approval-template");
    if (!slot || !template) return;
    slot.innerHTML = "";
    const node = template.content.firstElementChild.cloneNode(true);
    node.dataset.toolCallId = data.tool_call_id || "";
    fillField(node, "command", data.command || "");
    fillField(node, "intent", data.intent || "");
    const dangerEl = node.querySelector('[data-field="danger"]');
    const flags = data.danger_flags || [];
    if (dangerEl && flags.length) {
      dangerEl.hidden = false;
      dangerEl.textContent = "Danger: " + flags.join(", ");
    }
    const approve = node.querySelector('[data-role="approve"]');
    const reject = node.querySelector('[data-role="reject"]');
    approve.addEventListener("click", () => decide(data.tool_call_id, true, node));
    reject.addEventListener("click", () => decide(data.tool_call_id, false, node));

    // Refusal property (web.md §5): a stray Enter must never approve. The block is not a form, and
    // we additionally swallow Enter/Space so no keypress reaches a button as a default action.
    node.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
        e.preventDefault();
        e.stopPropagation();
      }
    });

    startCountdown(node, data.expires_in || 300);
    slot.appendChild(node);
    // Do not focus Approve. Move focus to the block itself (never a default action).
    node.setAttribute("tabindex", "-1");
    node.focus();
  }

  function decide(toolCallId, approve, node) {
    fetch("/approvals/" + encodeURIComponent(toolCallId), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approve: approve }),
    }).then(() => node.remove());
  }

  function startCountdown(node, seconds) {
    const el = node.querySelector('[data-field="countdown"]');
    if (!el) return;
    let remaining = seconds;
    const tick = () => {
      const m = Math.floor(remaining / 60);
      const s = remaining % 60;
      el.textContent = "Expires in " + m + ":" + String(s).padStart(2, "0");
      if (remaining <= 0) {
        el.textContent = "This proposal expired.";
        return;
      }
      remaining -= 1;
      node._countdown = setTimeout(tick, 1000);
    };
    tick();
  }

  // --- Emergency stop (web.md §3.1): confirm only that they meant it --------------------------
  function wireEstop() {
    const button = document.getElementById("estop");
    if (!button) return;
    button.addEventListener("click", () => {
      if (!window.confirm("Emergency stop the printer now?")) return;
      const printerId = button.dataset.printerId;
      fetch("/printers/" + encodeURIComponent(printerId) + "/estop", { method: "POST" });
    });
  }

  // --- Small DOM helpers ---------------------------------------------------------------------
  function appendMessage(role, text) {
    const el = document.createElement("div");
    el.className = "message role-" + role;
    el.dataset.role = role;
    const roleSpan = document.createElement("span");
    roleSpan.className = "message-role";
    roleSpan.textContent = role;
    const body = document.createElement("div");
    body.className = "message-body";
    const p = document.createElement("p");
    p.className = "block-text";
    p.textContent = text;
    body.appendChild(p);
    el.appendChild(roleSpan);
    el.appendChild(body);
    appendNode(el);
  }

  // Render an uploaded photo as a user message in the conversation thread, mirroring the
  // server-rendered image block (.message.role-user > .message-body > img.block-image).
  function appendImageMessage(artifactId) {
    const el = document.createElement("div");
    el.className = "message role-user";
    el.dataset.role = "user";
    const roleSpan = document.createElement("span");
    roleSpan.className = "message-role";
    roleSpan.textContent = "user";
    const body = document.createElement("div");
    body.className = "message-body";
    const img = document.createElement("img");
    img.className = "block-image";
    img.src = "/artifacts/" + encodeURIComponent(artifactId);
    img.alt = "attached image";
    body.appendChild(img);
    el.appendChild(roleSpan);
    el.appendChild(body);
    appendNode(el);
  }

  function appendNode(el) {
    const conversation = document.getElementById("conversation");
    if (!conversation) return;
    conversation.appendChild(el);
    conversation.scrollTop = conversation.scrollHeight;
  }

  function fillField(root, name, value) {
    const el = root.querySelector('[data-field="' + name + '"]');
    if (el) el.textContent = value;
  }

  function parseData(raw) {
    try {
      return JSON.parse(raw);
    } catch (e) {
      return { text: raw };
    }
  }

  function cssEscape(value) {
    return String(value).replace(/["\\]/g, "\\$&");
  }
})();
