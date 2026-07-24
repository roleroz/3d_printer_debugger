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
    const view = document.querySelector(".session-view");
    if (view) {
      const sessionId = view.dataset.sessionId;
      wireComposer(sessionId);
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

  // --- Camera capture: file input with capture=environment, preview + upload -----------------
  function wireCamera(sessionId) {
    const input = document.getElementById("camera-input");
    if (!input) return;
    input.addEventListener("change", () => {
      const file = input.files && input.files[0];
      if (!file) return;
      previewImage(file);
      uploadWithProgress(sessionId, file, "photo");
      input.value = "";
    });
  }

  function wireFileAttach(sessionId) {
    const input = document.getElementById("file-input");
    if (!input) return;
    input.addEventListener("change", () => {
      const file = input.files && input.files[0];
      if (!file) return;
      uploadWithProgress(sessionId, file, "file");
      input.value = "";
    });
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
  function uploadWithProgress(sessionId, file, kind) {
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
      if (label) {
        label.textContent =
          xhr.status === 413
            ? "File too large — rejected before upload finished."
            : xhr.status >= 400
            ? "Upload failed (" + xhr.status + ")."
            : "Uploaded " + (file.name || kind) + ".";
      }
      if (bar && xhr.status < 400) bar.value = 100;
      hideLater(wrap);
    });
    xhr.addEventListener("error", () => {
      if (label) label.textContent = "Upload failed.";
      hideLater(wrap);
    });
    xhr.send(file);
  }

  function hideLater(wrap) {
    if (wrap) setTimeout(() => (wrap.hidden = true), 2500);
  }

  // --- Audio recording: MediaRecorder, cap-and-submit, upload as an artifact (web.md §7) ------
  let mediaRecorder = null;
  let audioChunks = [];
  let audioCapTimer = null;

  function wireMic(sessionId) {
    const button = document.getElementById("mic-btn");
    if (!button || !navigator.mediaDevices || !window.MediaRecorder) return;
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
    // Transcription (Whisper) is server-side and still deferred; we upload and note it as pending.
    appendMessage("system", "Audio captured — transcription pending.");
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/sessions/" + encodeURIComponent(sessionId) + "/audio");
    xhr.setRequestHeader("Content-Type", "audio/webm");
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
