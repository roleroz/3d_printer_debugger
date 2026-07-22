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

## Decisions taken during implementation

- **[store] Timestamp precision is microseconds**, not milliseconds as an early draft implied.
  Millisecond precision let two back-to-back writes share a timestamp, making "latest"/"most
  recent" queries nondeterministic. Microseconds keep DB writes distinctly ordered; a `rowid`
  tiebreaker is also applied on timestamp orderings as belt-and-suspenders.
- **[store] The `busy_timeout` default is 5000 ms** (store.md §13 open question 3 left it to be
  chosen against measured transaction times; 5 s is a safe starting value, configurable via the
  `Database` constructor).
