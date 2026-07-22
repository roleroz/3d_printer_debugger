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

- **[orchestration] The Agent-SDK adapter is not implemented (T4.1, T7.1).** The turn loop
  (`turn.py`) drives an `AgentClient` seam and is fully tested with a fake client; the concrete
  adapter that wraps `claude-agent-sdk` (agent configuration with the four permission mechanisms,
  streaming, client release/resume/replay) is deferred and wired at the composition root.
- **[orchestration] The web-fetch SSRF guard (T4.3) is not implemented here.** The design places
  the loopback/private/link-local refusal on web fetch; whether the SDK enforces it or a fetch
  proxy is needed is an open question. Left unchecked; must be added before enabling web fetch in a
  cloud deployment.
- **[orchestration] The approval gate, binding, prompt assembly, turn loop, and startup recovery
  are fully implemented and tested** — including the security-critical gate paths (approve, reject,
  timeout-as-denial, crash-recovery-to-denial, no-bypass). This is the load-bearing security
  component and it is green.

## Decisions taken during implementation

- **[store] Timestamp precision is microseconds**, not milliseconds as an early draft implied.
  Millisecond precision let two back-to-back writes share a timestamp, making "latest"/"most
  recent" queries nondeterministic. Microseconds keep DB writes distinctly ordered; a `rowid`
  tiebreaker is also applied on timestamp orderings as belt-and-suspenders.
- **[store] The `busy_timeout` default is 5000 ms** (store.md §13 open question 3 left it to be
  chosen against measured transaction times; 5 s is a safe starting value, configurable via the
  `Database` constructor).
