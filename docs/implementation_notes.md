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
- **[kb] The real extraction model call is unverified.** `extraction._extract_section` calls the
  Anthropic API (small fast model, `claude-haiku-4-5`) with a strict JSON tool; it is lazy-imported
  and the hermetic tests inject a stub. **Action:** exercise it against the API with a key.
  `anthropic` is tracked in `requirements.txt` but not used by the hermetic suite.

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
- **[web] Still deferred in the UI shell:** server-side Whisper transcription (the clip
  uploads and is marked "transcription pending" — `faster-whisper` was chosen and should be added to
  `requirements.txt` with a transcription module when built, T7.1); real printer-strip data (the
  strip is structured with `data-field` hooks and shows placeholder temps/status until the printer
  client publishes onto the session stream); and true streamed uploads with post-upload type
  validation (the body is currently read whole before being stored, T6.1).

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

## Decisions taken during implementation

- **[store] Timestamp precision is microseconds**, not milliseconds as an early draft implied.
  Millisecond precision let two back-to-back writes share a timestamp, making "latest"/"most
  recent" queries nondeterministic. Microseconds keep DB writes distinctly ordered; a `rowid`
  tiebreaker is also applied on timestamp orderings as belt-and-suspenders.
- **[store] The `busy_timeout` default is 5000 ms** (store.md §13 open question 3 left it to be
  chosen against measured transaction times; 5 s is a safe starting value, configurable via the
  `Database` constructor).
