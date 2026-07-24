# Implementation notes — items needing attention

This file records work that could not be completed or verified autonomously and needs the
maintainer's attention, plus decisions taken during implementation that deviate from or refine the
design. It is maintained across the module commits on the `feature/implementation` branch.

## Environment and build

- **Build system:** Bazel (Bzlmod), `rules_python` with a hermetic Python 3.12 toolchain. Run
  `bazel test //...` for the full suite. Live/hardware tests are tagged `manual` +
  `requires-network` and are excluded from the default run (see `.bazelrc`).
- **Dependencies:** `requirements.txt` is the human source of truth; a `requirements.lock.txt`
  (generated) is what Bazel's `pip.parse` consumes. Modules that need third-party packages wire
  them in as they are added. The lock now includes `claude-agent-sdk` and its full transitive
  closure (`mcp`, `httpx-sse`, `jsonschema` (+`attrs`, `jsonschema-specifications`, `referencing`,
  `rpds-py`), `pydantic-settings`, `pyjwt`, `python-dotenv`, `sse-starlette`, `cryptography`+`cffi`+
  `pycparser`, `sniffio`), generated with pip against the real package. `google-cloud-storage` and
  `anthropic` remain in `requirements.txt` but out of the lock (exercised only against their live
  services).

## Needs the maintainer

- **[store] Object-storage backend (T3.6) is unverified.** `ObjectStorageArtifactStore` (GCS) is
  implemented with a lazy `google-cloud-storage` import, but it has **no automated test** — the
  design says object storage is "tested against the real service or not at all," and no bucket or
  credentials are available here. **Action:** run it against a real GCS bucket and add a
  `manual`/`requires-network` live test. `google-cloud-storage` is listed in `requirements.txt`
  but is not exercised by the hermetic suite.

- **[kb] Live Moonraker config import (T4.3) is a seam, not wired.** `config_import.py` defines a
  `LiveConfigProvider` protocol and `import_live_config`, but the concrete Moonraker-backed
  provider belongs to the printer-access module (module 4) and is wired at the composition root.
  T4.3 is left unchecked in `kb_ingestion.md` until then.
- **[kb] Section extraction is re-backed onto the Agent SDK (DONE).**
  `extraction._extract_section` now classifies each section via a small cached `claude-agent-sdk`
  query (`claude-haiku-4-5`, strict JSON via the SDK's `output_format` json-schema →
  `ResultMessage.structured_output`, with a text-parse fallback), authenticated with the process-env
  `CLAUDE_CODE_OAUTH_TOKEN` (no `ANTHROPIC_API_KEY`, no `anthropic` package). It stays synchronous
  and self-contained: when a request event loop is already running it drives the async query in a
  worker-thread `asyncio.run`. The hermetic tests inject a stub via the `_extract_section` seam and
  cover the pure `_parse_extraction`/`_dict_to_extraction` helpers, so the SDK is never imported in
  tests. ([decisions.md 2026-07-23 "KB section extraction routes through the Agent SDK"].)
  **Still needs the maintainer:** one live run to confirm the real haiku call returns the expected
  JSON shape (the SDK is never invoked hermetically).

- **[indexing] Background index build (T4.6) is not wired.** `index_status` exists and reports
  ready, but building the G-code index in the background with progress reporting is an
  orchestration concern (threads/tasks) wired at the composition root; left unchecked in
  `file_indexing.md`.
- **[indexing] Object attribution uses slicer markers only; the geometric fallback is not
  implemented.** The fixture emits `; printing object` markers, so attribution is exact. The
  fallback (attribute by plate-layout footprint when markers are absent) has no marker-less fixture
  to test against and is deferred. **Action:** add a marker-less G-code fixture and the fallback.
- **[indexing] The MCP wrapping (`mcp.py`) is fleshed out; the SDK registration line is exercised
  only live.** `build_sdk_server(name, instance)` wraps every public tool method: it derives a
  minimal JSON input schema from the method signature (`input_schema`, resolving PEP-563 string
  annotations), calls the sync method with the decoded args, and returns the bounded dict as MCP
  text content (`format_result`), turning a `ToolError` into an MCP error result (`format_error`).
  The bare method name is passed to the SDK's `tool()`, so the tool qualifies as
  `mcp__{server}__{method}` — matching the allowlist (confirmed against the installed SDK). These
  pure adapters are hermetically tested (`servers_test.py::McpAdapterTest`); the single
  `create_sdk_mcp_server` call is lazy-imported and runs only on the live path.
- **[indexing] Layers are inferred from extruding-Z increases**, since this OrcaSlicer export emits
  no explicit layer-change comments. If a future file carries `; CHANGE_LAYER`/`; Z_HEIGHT`
  markers, preferring them would be more robust.

- **[printer] The Moonraker client is HTTP-only; the persistent WebSocket subscription is a
  refinement.** Reads, reachability, and command submission work over HTTP (stdlib `urllib`). The
  design's §2.1 persistent WebSocket (for always-current live state and print-progress observation)
  is not implemented; HTTP polling covers the same data. **Action:** add the WebSocket subscription
  if push latency matters. The live read-only integration test **passed against the real printer**
  (`voron2.eterovic.xyz`); write and emergency-stop paths are unit-tested against a fake transport
  only and were never fired at the real machine.
- **[printer] Runtime-state capture as an artifact (T4.3) returns bytes/metadata; the store write
  is wired at the composition root** (the tool reports availability and byte count; persisting the
  webcam/printer-state blob belongs to the orchestrator).
- **[kb→printer] The live-config provider is implemented** (`printer/live_config.py`) and satisfies
  the KB `LiveConfigProvider` seam, but calling `import_live_config` during ingest is **NOT yet
  wired at the composition root** — `import_live_config` is referenced nowhere in
  `composition.py`/`main.py`. So an ingest today imports Klipper config only from the local paths
  the markdown references (`_import_local`), which are absent inside the container → the affected
  printers come back `degraded`. **Action:** wire `import_live_config` into the ingest path so a
  reachable printer's config is pulled from Moonraker. (Consistent with the "KB document is not
  auto-ingested" note below.)

- **[orchestration] The Agent-SDK adapter is now wired end-to-end (T4.1).** `sdk_config.py` (the
  four permission mechanisms), `sdk_translate.py` (SDK message → `AgentEvent`), and the permission
  decision in `sdk_client.py` (gate the write, deny unlisted, never ask about a denied tool) remain
  **hermetically tested** without pulling the SDK. On top of them, `printer_debugger/composition.py`
  now wires the whole turn: `build_servers` builds the per-session in-process MCP servers,
  `build_prompt` assembles the system prompt from the procedure catalog + the bound printer's KB
  view + session state, and `make_approve` bridges the SDK permission callback to the `ApprovalGate`
  (publish the proposal onto the session's SSE stream → await the human via `POST /approvals/{id}` →
  `gate.resolve` → record). `main.py` composes it. **Credentials:** the subscription OAuth token is
  read from `CLAUDE_CODE_OAUTH_TOKEN` (confirmed by inspecting the installed
  `claude-agent-sdk` 0.2.126 — it reads that name from `ClaudeAgentOptions.env` or the process env;
  create one with `claude setup-token`). `composition.require_oauth_token` crashes at startup if it
  is absent (no API-key fallback), and passes it to the SDK via `ClaudeAgentOptions.env`.
  `PD_MODEL`/`PD_EFFORT` configure the model (default `claude-opus-4-8`) and effort (default
  `medium`). **Execution locus of a printer write ([decisions.md 2026-07-23]):** the gate *decides
  only* — its `execute` is a no-op; the `propose_command` MCP tool performs the single actual
  submission to Moonraker (`make_command_submitter`), on the approved path, so a command reaches the
  machine exactly once (regression-tested in `composition_test.py`). **Still needs the maintainer:**
  a `manual` live test with real subscription credentials (the SDK is never invoked hermetically);
  and session release-on-idle / replay-on-resume-failure (T7.1) — only the `resume=` seam is wired.
- **[orchestration] The web-fetch SSRF guard (T4.3) was intentionally dropped**
  ([decisions.md 2026-07-23 "Web-fetch SSRF guard dropped"]). WebFetch/WebSearch are Anthropic
  **server tools** — they execute on Anthropic infrastructure, not on the host — so the
  LAN-bypass threat that motivated a loopback/private/link-local refusal does not exist for this
  architecture. No guard is needed, including in a cloud deployment. (This supersedes the earlier
  "must be added before enabling web fetch" note.)
- **[orchestration] The approval gate, binding, prompt assembly, turn loop, and startup recovery
  are fully implemented and tested** — including the security-critical gate paths (approve, reject,
  timeout-as-denial, crash-recovery-to-denial, no-bypass). This is the load-bearing security
  component and it is green.

- **[web] The UI shell is now built over the existing backend routes.** Server-rendered HTML pages
  (`GET /` session list, `GET /sessions/{id}` session view) are rendered with plain Python string
  templates plus `html.escape` (`web/templates.py`) — no templating engine, no bundler. The
  hand-written client JS (`web/static/app.js`, served with `web/static/styles.css` from a
  `/static/{name}` route backed by Bazel `data` files) covers the four browser capabilities:
  camera capture (`capture="environment"` file input, preview + upload), audio recording
  (`MediaRecorder`, two-minute cap-and-submit), upload progress (XHR progress bar for large
  `.3mf`/G-code), and the SSE consumer (`EventSource` with position-based `last_id` reconnect and
  optimistic render of the user's own message). The approval interface renders the command verbatim
  in monospace with danger flags and a five-minute countdown; its refusal properties are baked into
  the markup and JS (Reject precedes Approve, Approve has no `autofocus`/default focus, and a
  keydown guard swallows Enter/Space so a stray keypress cannot approve) and are covered by
  `web/ui_test.py`. Artifact serving (`GET /artifacts/{id}`) streams the blob with its stored
  content-type (404 when the row or blob is missing); this required adding an `ArtifactStore` to
  `AppContext`, now wired in `main.py`. Uploads persist into the artifact store and are classified
  by content-type (image → photo, audio → audio). The JSON reads moved to `GET /api/sessions` and
  `GET /api/sessions/{id}`; the mutating routes still return JSON. The OIDC integration is still an
  `X-Auth-Subject` seam; a real OIDC middleware validates and sets it.
- **[web] The live Claude-agent conversation is now wired.** `AppContext.on_message` runs a turn via
  the turn loop against the `ClaudeAgentClient`, and each event is streamed to the session's SSE hub
  (`assistant` text frames, `tool` activity frames) so the browser renders replies as they arrive.
  `POST /sessions/{id}/messages` delegates to the handler (which persists the user message); it only
  persists the message itself as a fallback when no handler is wired, so a message is never stored
  twice. The upload route (`POST /sessions/{id}/files`) now classifies the file by its `X-Filename`
  extension (`.gcode`→G-code with an index, `.3mf`→project stored whole) and calls an injected
  `on_upload` hook that builds and stores the G-code index synchronously while the request is open.
  SSE `data` frames carry JSON objects (the client `JSON.parse`s them, and `app.py::_frame`
  `json.dumps`es the payload), so publishers pass dicts.
- **[web] Server-side Whisper transcription is DONE.** `web/transcription.py` (`Transcriber` +
  `TranscribeAudio` seam) loads a bundled `faster-whisper` `base` model (`local_files_only`) once;
  `/audio` runs it via `asyncio.to_thread`, stores the transcript as the artifact note, and feeds it
  into the session through `on_message` (same path as the text composer). The `base` model is pinned
  in `MODULE.bazel` as `http_file` repos (HuggingFace `Systran/faster-whisper-base`) and packed into
  the image at `/app/whisper-base/*`; imports are lazy and the model stays out of `bazel test`.
  **Still needs the maintainer:** a live check that a real recorded clip transcribes in the
  container (the model is never loaded in tests — a fake transcriber is injected).
- **[web] The auth/CSRF gate is pure-ASGI, not `BaseHTTPMiddleware`.** `@app.middleware("http")`
  (Starlette `BaseHTTPMiddleware`) routes every response through an anyio memory stream + task
  group, which stalled large upload responses (upload reached 100% then hung with no reply), broke
  long-lived SSE streams, and dumped `CancelledError` on shutdown. Replaced with `AuthCsrfMiddleware`
  (raw ASGI, inspects only scope headers) so request/response byte streams flow straight through
  uvicorn. Auth/CSRF behaviour is unchanged and covered by the existing 401/403/allowed tests.
- **[web] Self-signed HTTPS is on by default so browser mic recording works over the LAN.**
  `getUserMedia`/`MediaRecorder` are exposed only in a secure context (HTTPS or `localhost`), so a
  phone hitting `http://<laptop-ip>:8080` had a silently dead mic button. `web/tls.py` resolves the
  cert/key from the environment (`resolve_tls`, a pure function) and generates a self-signed pair
  (`ensure_self_signed`, `cryptography` imported lazily). Env contract: `PD_TLS=off` forces plain
  HTTP; `PD_TLS_CERT`+`PD_TLS_KEY` (both set) use those files as-is; otherwise an auto pair is
  generated at `<data_dir>/tls/cert.pem`+`key.pem`. The cert lives under the mounted data dir, so it
  persists across restarts (regenerated only if absent). Its SAN includes the `PD_ADVERTISE_HOST`/
  `PD_HOST` addresses plus `localhost`/`127.0.0.1`/`::1` (IPs as `IPAddress` entries — iOS requires
  the address in the SAN). `main.py` passes `ssl_certfile`/`ssl_keyfile` to `uvicorn.run`; `--check`
  neither generates a cert nor serves. Access becomes `https://<ip>:8080`, and the phone must accept
  a one-time certificate warning before the mic works. `cryptography` (already locked) is depended on
  only by `//printer_debugger:main` and the `tls_test` target, staying out of the rest of the suite.
  The mic button no longer dies silently on an insecure origin: `wireMic` in `app.js` attaches a
  handler that posts a system message explaining the HTTPS requirement (or missing `MediaRecorder`).
- **[web] Still deferred in the UI shell:** real printer-strip data (the strip is structured with
  `data-field` hooks and shows placeholder temps/status until the printer client publishes onto the
  session stream); and true streamed uploads with post-upload type validation (the body is currently
  read whole before being stored, T6.1).

- **[container] The OCI image builds via Bazel; the no-system-python case is verified.**
  `bazel build //:image` succeeds (rules_oci, `debian:12-slim` base pinned by digest). The binary
  uses the rules_python **`script` (bash) bootstrap** (`.bazelrc`) so it execs the bundled hermetic
  interpreter directly rather than a stage-1 `#!/usr/bin/env python3` launcher — debian-slim has no
  `python3`. This was verified by running the binary with `python3` removed from PATH
  (`env -i PATH=<no-python> main --check` → "build check OK"). The only step not run here is the
  actual `bazel run //:load` + `docker run` (Docker daemon socket was permission-denied); the
  interpreter-without-system-python risk that would have caused is now covered.
- **[container] `main.py` is the composition root and now wires the agent.** It builds the store +
  artifact store + web app, requires the OAuth token (crashing if absent), runs startup recovery
  (`sweep_interrupted_tool_calls`, `gate.recover_pending`), and wires `on_message` (the turn loop),
  `on_upload` (the synchronous index build), `resolve_approval` (`gate.resolve`), and
  `emergency_stop` (`M112` at the bound printer). Live providers still needing the maintainer: the
  KB document is not auto-ingested (a `KbIngester` is constructed and `assemble_view` serves any
  printer already in the store, but the watcher/ingest loop and the live Moonraker config provider
  are not run here yet), and the persistent printer WebSocket subscription is not wired.
- **[build] Keeping the SDK out of `bazel test`.** `claude-agent-sdk` and its full transitive
  closure are locked in `requirements.lock.txt`, but every `claude_agent_sdk` import is lazy (inside
  functions), so no library carries a Bazel dependency on it. Only `//printer_debugger:main` (the
  py_binary, for the image) declares `@pypi//claude_agent_sdk`. `.bazelrc` adds
  `test --build_tests_only`, so `bazel test //...` builds only test targets and their deps — not
  `main` or `//:image` — and therefore never fetches the ~85 MB wheel. Verified two ways:
  `bazel cquery 'somepath(kind(".*_test", //...), @pypi//claude_agent_sdk)'` is empty, and a full
  `bazel test //...` run builds no OCI/image actions. `bazel build //:image` still builds the image
  (and fetches the SDK) explicitly.

- **[build] Bundling the faster-whisper `base` model hermetically.** The transcription path
  ([decisions.md 2026-07-23 voice transcription]) must run offline in the container, so the model
  is never downloaded at runtime. The four CTranslate2 model files (`config.json`, `model.bin`,
  `tokenizer.json`, `vocabulary.txt`, ~140 MB total, `model.bin` dominates) are pinned in
  `MODULE.bazel` as `http_file` repos, each by `sha256`, against a fixed
  HuggingFace revision of `Systran/faster-whisper-base`
  (`ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66`). The root `BUILD.bazel` packs them into a
  `pkg_tar` (`:whisper_model_layer`) at `/app/whisper-base/*` and adds that layer to `:image`.
  `main.py` points `Transcriber` at `PD_WHISPER_MODEL_DIR` (default `/app/whisper-base`), and the
  `Transcriber` loads with `local_files_only=True` so it never reaches the network. Only the image
  references `:whisper_model_layer`, and `faster_whisper` is depended on only by
  `//printer_debugger:main` with every import lazy, so `bazel test //...` (with
  `--build_tests_only`) fetches neither the model files nor the heavy wheels
  (`ctranslate2`/`onnxruntime`/`av`/`tokenizers`), mirroring the `claude-agent-sdk` handling.
  To re-pin after a model update, re-download each file from the new revision and update its
  `sha256` in `MODULE.bazel`.

## Decisions taken during implementation

- **[store] Timestamp precision is microseconds**, not milliseconds as an early draft implied.
  Millisecond precision let two back-to-back writes share a timestamp, making "latest"/"most
  recent" queries nondeterministic. Microseconds keep DB writes distinctly ordered; a `rowid`
  tiebreaker is also applied on timestamp orderings as belt-and-suspenders.
- **[store] The `busy_timeout` default is 5000 ms** (store.md §13 open question 3 left it to be
  chosen against measured transaction times; 5 s is a safe starting value, configurable via the
  `Database` constructor).
