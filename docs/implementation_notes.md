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
  them in as they are added.

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
- **[indexing] The MCP wrapping (`mcp.py`) is unverified.** It lazy-imports `claude-agent-sdk` and
  wraps the tool methods; the tool *logic* is fully tested, but the SDK registration is exercised
  only live. `claude-agent-sdk` is tracked in `requirements.txt`.
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
  the KB `LiveConfigProvider` seam; calling `import_live_config` during ingest is wired at the
  composition root.

- **[orchestration] The Agent-SDK adapter's core is built and tested; end-to-end wiring remains
  (T4.1, T7.1).** `sdk_config.py` (the four permission mechanisms — allow/deny tool lists, deny-
  unlisted, the gated write), `sdk_translate.py` (SDK message → `AgentEvent`), and the permission
  decision in `sdk_client.py` (gate the write, deny unlisted, never ask about a denied tool) are
  **hermetically tested** without pulling the 273 MB SDK. `ClaudeAgentClient.run_turn` drives
  `claude-agent-sdk` (lazy import; live path). **Remaining before conversations work end-to-end:**
  (1) add `claude-agent-sdk` to `requirements.lock.txt` and make `//printer_debugger:main` depend
  on it so the image carries it (the ~273 MB lands only in the image, not `bazel test //...`);
  (2) build the per-session in-process MCP servers by wrapping `ProjectTools`/`GcodeTools`/
  `PrinterTools` with `create_sdk_mcp_server`, and wire `build_servers`/`build_prompt`/`approve`
  (approve → the `ApprovalGate`) at the composition root; (3) a `manual` live test with subscription
  credentials. Session resume/replay (T7.1) is the `resume=` seam, wired with (2).
- **[orchestration] The web-fetch SSRF guard (T4.3) is not implemented here.** The design places
  the loopback/private/link-local refusal on web fetch; whether the SDK enforces it or a fetch
  proxy is needed is an open question. Left unchecked; must be added before enabling web fetch in a
  cloud deployment.
- **[orchestration] The approval gate, binding, prompt assembly, turn loop, and startup recovery
  are fully implemented and tested** — including the security-critical gate paths (approve, reject,
  timeout-as-denial, crash-recovery-to-denial, no-bypass). This is the load-bearing security
  component and it is green.

- **[web] The backend routes, security, SSE fan-out, and startup are implemented and tested; the
  HTML/JS presentation layer is not.** The FastAPI app exposes JSON routes with the auth + CSRF
  middleware (the cross-site-POST-to-approvals refusal is tested), SSE fan-out with
  position-based reconnect, upload Content-Length pre-check, the emergency-stop route, and startup
  URL printing. **Not implemented (deferred):** server-rendered HTML pages (T4.1/T4.2), the client
  JavaScript for camera/mic/upload-progress/SSE-consumer and the approval refusal UI where Enter
  must not approve (T4.3, T5.1 UI half), audio recording + Whisper transcription (T7.1), true
  streamed uploads to the artifact store (T6.1 does the size pre-check only), and artifact serving
  (T4.4). The OIDC integration is represented by an `X-Auth-Subject` seam; a real OIDC middleware
  validates and sets it.
- **[web] Local Whisper transcription is not implemented.** `faster-whisper` was chosen but audio
  capture/transcription (T7.1) is deferred with the client JS; add `faster-whisper` to
  requirements and a transcription module when building it.

- **[container] The OCI image builds via Bazel; the no-system-python case is verified.**
  `bazel build //:image` succeeds (rules_oci, `debian:12-slim` base pinned by digest). The binary
  uses the rules_python **`script` (bash) bootstrap** (`.bazelrc`) so it execs the bundled hermetic
  interpreter directly rather than a stage-1 `#!/usr/bin/env python3` launcher — debian-slim has no
  `python3`. This was verified by running the binary with `python3` removed from PATH
  (`env -i PATH=<no-python> main --check` → "build check OK"). The only step not run here is the
  actual `bazel run //:load` + `docker run` (Docker daemon socket was permission-denied); the
  interpreter-without-system-python risk that would have caused is now covered.
- **[container] `main.py` is the composition root and is intentionally minimal.** It wires the
  store + web app + local auth and serves. The Agent-SDK adapter, the printer/KB live providers,
  the MCP servers, and the orchestrator turn loop are **not yet wired into it** — those are the
  deferred adapters noted above. A full deployment wires them here.

## Decisions taken during implementation

- **[store] Timestamp precision is microseconds**, not milliseconds as an early draft implied.
  Millisecond precision let two back-to-back writes share a timestamp, making "latest"/"most
  recent" queries nondeterministic. Microseconds keep DB writes distinctly ordered; a `rowid`
  tiebreaker is also applied on timestamp orderings as belt-and-suspenders.
- **[store] The `busy_timeout` default is 5000 ms** (store.md §13 open question 3 left it to be
  chosen against measured transaction times; 5 s is a safe starting value, configurable via the
  `Database` constructor).
