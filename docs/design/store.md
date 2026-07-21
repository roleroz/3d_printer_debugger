# Store — module design

The store is the system's record of truth. Every other module reads and writes through it, so it
is designed first ([architecture.md §14](../architecture.md#14-module-design-documents-to-follow)).

It has two halves behind one module boundary: **structured data** in SQLite, and **artifacts** on
a blob interface whose implementation differs between local and cloud deployments.

Requirements come from [spec.md §10](../spec.md#10-data-and-persistence-requirements); the
component's place in the system is [architecture.md §3.8](../architecture.md#38-store) and its
entities are [architecture.md §4](../architecture.md#4-data-model).

## 1. Scope

**In scope:** the schema and its constraints, the interfaces other modules use, artifact key
layout, the connection and concurrency discipline, migrations, backup and restore, and storage
accounting.

**Out of scope:** what other modules choose to store. The store owns durability and shape; it
does not own meaning. It contains no printer logic, no G-code knowledge, and no agent concepts
beyond the columns needed to record them.

## 2. Design decisions

### 2.1 Deployment shape resolves the deferred question

The GCP deployment runs the container on a **Compute Engine VM with a persistent disk**, not on
Cloud Run. This resolves the question [architecture.md §9](../architecture.md#9-deployment)
deferred here.

Cloud Run is a request-scoped autoscaler, and four of its properties are wrong for this system:

| Property | Consequence here |
| --- | --- |
| Bounded request timeout | Caps SSE streams and blocked approval waits, which can be long |
| Scale to zero | Discards the in-memory agent client a live session depends on |
| More than one instance | Breaks the single-writer discipline SQLite depends on |
| No persistent local disk | The database file has nowhere to live |

The alternatives each cost more than they save. Postgres behind the store interface means every
query must work on two engines and both must be tested, for a single-user workload SQLite handles
without effort. SQLite on a network filesystem is worse: WAL is unreliable over NFS and file
locking is the classic corruption path, so the failure mode is a damaged database rather than an
error. A small always-on VM removes all four problems and keeps local and cloud running the same
code against the same engine.

### 2.2 Hand-written SQL migrations

Numbered `.sql` files applied in order, with the applied version recorded in the database. No
migration framework. The schema is small enough that the whole history stays readable, and the
migration that matters is the one someone can read in full before running it against data they
cannot regenerate.

### 2.3 Raw SQL with typed row mappers

Hand-written SQL through the standard-library driver, with explicit functions mapping rows to
frozen dataclasses. No ORM and no query builder.

The reason is the write path. The single-writer discipline in [§7](#7-concurrency) is only
verifiable if every statement is visible where it runs; an ORM that decides when to flush puts a
layer between the code and the thing being disciplined. The cost is that mapping is written by
hand, which at this schema size is bounded and mechanical.

## 3. Module boundary

Two interfaces. Callers depend on these, never on SQL or on a filesystem path.

### 3.1 `StructuredStore`

Typed methods over the entities in [§4](#4-schema), grouped by entity: create, fetch, list, and
the few targeted updates the spec requires (rename a session, close it, rebind its printer,
accumulate usage). It returns frozen dataclasses, never rows or dicts.

It deliberately exposes **no general query method**. A caller that needs a new query gets a new
named method, so every access pattern is visible in one file and can be indexed for.

### 3.2 `ArtifactStore`

| Operation | Purpose |
| --- | --- |
| `put` | Consume a byte stream, return the key it was stored under |
| `open` | Return a readable stream for a key |
| `exists` | Whether a key is present |
| `size` | Size in bytes of one key |
| `total_size` | Sum over all keys, for storage accounting ([§10](#10-storage-accounting)) |

Streaming in both directions is required, not optional: a G-code upload can reach the size limit
in [spec.md §5.4](../spec.md#54-file-ingestion) and must never be held in memory whole.

There is **no delete**. Sessions cannot be deleted in this version, so no artifact ever becomes
unreferenced; adding deletion later is a change to this interface and to the retention model
together, not a quiet addition.

Two implementations: local filesystem, and object storage for the cloud deployment. This is the
substitutable seam [spec.md §10](../spec.md#10-data-and-persistence-requirements) requires.

## 4. Schema

Conventions across every table:

- **Primary keys** are text: a short type prefix plus a UUID (`ses_`, `prn_`, `art_`). The prefix
  costs nothing and makes an identifier in a log line self-describing.
- **Timestamps** are ISO-8601 UTC text with a `Z` suffix. SQLite has no date type, and this
  format sorts lexicographically, which is what ordering needs.
- **Structured values** are JSON in `TEXT` columns, queryable through SQLite's JSON functions
  where needed.
- **Enumerations** are `CHECK` constraints. SQLite has no enum type, and a constraint fails at
  write time rather than surfacing an unexpected value at read time.
- `PRAGMA foreign_keys = ON` on every connection; SQLite does not enforce them otherwise.

### 4.1 `schema_version`

```sql
CREATE TABLE schema_version (
    version    INTEGER NOT NULL PRIMARY KEY,
    applied_at TEXT    NOT NULL
);
```

One row per applied migration, so the history is visible rather than just the current number.

### 4.2 `printer`

```sql
CREATE TABLE printer (
    id              TEXT NOT NULL PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    address         TEXT,
    config_path     TEXT,
    kb_section      TEXT NOT NULL,
    kb_content_hash TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('complete', 'degraded')),
    missing         TEXT,
    absent_since    TEXT,
    ingested_at     TEXT NOT NULL
);
```

`address` and `config_path` are nullable because
[spec.md §5.1](../spec.md#51-printer-management) requires a printer with a missing required value
to stay usable in a degraded form. `status` and `missing` (a JSON array naming what could not be
found) carry that state explicitly, so a caller cannot mistake absence for "not looked up yet".
`degraded` means the document did not supply everything, not that the printer cannot work: a
printer with an address but no local config path is degraded, yet still gets its configuration
live from Moonraker when reachable
([kb_ingestion.md §3.4](kb_ingestion.md#34-completeness-and-missing-values)).

`absent_since` is null while the printer is present in the current document. When a printer's
section is removed from the document, the row is kept — its sessions still reference it — and this
records when it went missing, so the printer list can show it as no longer maintained rather than
letting a removed printer look current
([kb_ingestion.md §3.1](kb_ingestion.md#31-change-detection-and-removal)).

`kb_section` holds the printer's raw prose verbatim — it is what the orchestrator puts in the
system prompt. `kb_content_hash` keys the extraction cache: unchanged hash means no re-extraction.

### 4.3 `config_snapshot`

```sql
CREATE TABLE config_snapshot (
    id            TEXT NOT NULL PRIMARY KEY,
    printer_id    TEXT NOT NULL REFERENCES printer(id),
    source        TEXT NOT NULL CHECK (source IN ('files', 'moonraker')),
    captured_at   TEXT NOT NULL,
    contents      TEXT NOT NULL,
    discrepancies TEXT
);

CREATE INDEX idx_config_snapshot_printer ON config_snapshot (printer_id, captured_at DESC);
```

Snapshots accumulate rather than overwrite; the latest is a query, and the history is what makes
"this value changed when?" answerable. `source` is mandatory because provenance is a requirement
([spec.md §5.1](../spec.md#51-printer-management)), not a nicety — a value from a live read and a
value from a file are different claims.

`discrepancies` holds the disagreements found at ingest, recorded but not announced, per the same
section.

### 4.4 `session`

```sql
CREATE TABLE session (
    id                    TEXT    NOT NULL PRIMARY KEY,
    name                  TEXT    NOT NULL,
    printer_id            TEXT    REFERENCES printer(id),
    state                 TEXT    NOT NULL CHECK (state IN ('open', 'closed')),
    sdk_session_id        TEXT,
    created_at            TEXT    NOT NULL,
    last_active_at        TEXT    NOT NULL,
    closed_at             TEXT,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX idx_session_last_active ON session (last_active_at DESC);
```

`printer_id` is nullable only for the window between creating a session and binding its printer
([spec.md §5.2](../spec.md#52-session-lifecycle)); it is set as soon as detection or the user
resolves it.

`sdk_session_id` is the Agent SDK's own identifier, kept so a session can be resumed against the
SDK's stored state. It is a pointer to a cache, not the conversation itself — the conversation
lives in `message`.

**Token counts are stored; cost is not.** Cost is computed at display time from configured
pricing, so a price change does not leave stale money in the database
([spec.md §12](../spec.md#12-non-functional-requirements)).

### 4.5 `printer_binding`

```sql
CREATE TABLE printer_binding (
    id         TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES session(id),
    printer_id TEXT NOT NULL REFERENCES printer(id),
    bound_at   TEXT NOT NULL,
    reason     TEXT NOT NULL CHECK (reason IN ('detected', 'chosen', 'reassigned'))
);

CREATE INDEX idx_printer_binding_session ON printer_binding (session_id, bound_at);
```

[spec.md §5.2](../spec.md#52-session-lifecycle) requires a reassignment to be recorded so findings
established beforehand are not silently reattributed. A history table is the only way to answer
"which printer was this session about when that conclusion was reached" — a single column on
`session` cannot.

### 4.6 `message`

```sql
CREATE TABLE message (
    id         TEXT    NOT NULL PRIMARY KEY,
    session_id TEXT    NOT NULL REFERENCES session(id),
    seq        INTEGER NOT NULL,
    role       TEXT    NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    UNIQUE (session_id, seq)
);
```

`content` is the full JSON block list, not flattened text — thinking blocks, tool-use blocks, and
image references all have to survive a round trip.

`seq` gives deterministic ordering within a session. Timestamps are not sufficient: two messages
can share one, and clock adjustment must not reorder a conversation.

### 4.7 `tool_call`

```sql
CREATE TABLE tool_call (
    id             TEXT    NOT NULL PRIMARY KEY,
    session_id     TEXT    NOT NULL REFERENCES session(id),
    message_id     TEXT    REFERENCES message(id),
    server         TEXT    NOT NULL,
    tool           TEXT    NOT NULL,
    arguments      TEXT    NOT NULL,
    result_summary TEXT,
    is_error       INTEGER NOT NULL DEFAULT 0,
    started_at     TEXT    NOT NULL,
    finished_at    TEXT
);

CREATE INDEX idx_tool_call_session ON tool_call (session_id, started_at);
```

`result_summary` is a bounded description, not the payload. A G-code extraction can return a large
result, and the record needs to say what was asked and roughly what came back — the payload itself
is reproducible by re-running the query against the retained artifact.

`finished_at` stays null while a call is in flight, which is how an interrupted process is
recognised on restart ([§11](#11-failure-handling)).

### 4.8 `approval`

```sql
CREATE TABLE approval (
    id                TEXT NOT NULL PRIMARY KEY,
    tool_call_id      TEXT NOT NULL UNIQUE REFERENCES tool_call(id),
    proposed_command  TEXT NOT NULL,
    danger_flags      TEXT,
    decision          TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'timed_out')),
    decided_by        TEXT NOT NULL,
    decided_at        TEXT NOT NULL
);
```

This table is the audit trail [spec.md §11](../spec.md#11-security-and-privacy-requirements)
requires: every printer write attributable to a specific approval.

`proposed_command` is stored **verbatim, on the approval row**, rather than being read back from
the tool call's arguments. The record must show exactly what the user was shown when they decided.
Reconstructing it later from arguments would make the audit trail depend on formatting code that
may since have changed.

`UNIQUE` on `tool_call_id` makes double-approval of one proposal impossible at the schema level.
`timed_out` is a decision, not a missing row, because a timeout is a denial with a cause
([architecture.md §5.3](../architecture.md#53-printer-write-with-approval)).

### 4.9 `artifact`

```sql
CREATE TABLE artifact (
    id           TEXT    NOT NULL PRIMARY KEY,
    session_id   TEXT    NOT NULL REFERENCES session(id),
    kind         TEXT    NOT NULL CHECK (kind IN (
                     'project', 'gcode', 'photo', 'webcam_still',
                     'audio', 'procedure_output', 'printer_state')),
    blob_key     TEXT    NOT NULL UNIQUE,
    size_bytes   INTEGER NOT NULL,
    content_type TEXT    NOT NULL,
    captured_at  TEXT    NOT NULL,
    note         TEXT
);

CREATE INDEX idx_artifact_session ON artifact (session_id, captured_at);
```

`printer_state` is how captured live state persists.
[architecture.md §4](../architecture.md#4-data-model) deliberately gives runtime state no table
of its own; when a session records it, it becomes an artifact — a timestamped observation, not a
current value something might later mistake for live data.

### 4.10 `file_index`

```sql
CREATE TABLE file_index (
    id             TEXT    NOT NULL PRIMARY KEY,
    artifact_id    TEXT    NOT NULL UNIQUE REFERENCES artifact(id),
    kind           TEXT    NOT NULL CHECK (kind IN ('project', 'gcode')),
    blob_key       TEXT    NOT NULL UNIQUE,
    format_version INTEGER NOT NULL,
    built_at       TEXT    NOT NULL
);
```

`format_version` is what makes an index disposable. When the indexer's format changes, the stored
index is stale rather than wrong: it is discarded and rebuilt from the retained artifact
([architecture.md §8](../architecture.md#8-failure-and-recovery)).

### 4.11 `procedure_result`

```sql
CREATE TABLE procedure_result (
    id          TEXT NOT NULL PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES session(id),
    printer_id  TEXT NOT NULL REFERENCES printer(id),
    procedure   TEXT NOT NULL CHECK (procedure IN (
                    'input_shaper', 'pid_tune', 'first_layer',
                    'pressure_advance_flow', 'temperature', 'stringing_retraction')),
    filament    TEXT,
    values_json TEXT NOT NULL,
    evidence    TEXT,
    recorded_at TEXT NOT NULL,
    CHECK (
        (procedure IN ('input_shaper', 'pid_tune') AND filament IS NULL)
        OR
        (procedure NOT IN ('input_shaper', 'pid_tune') AND filament IS NOT NULL)
    )
);

CREATE INDEX idx_procedure_result_scope ON procedure_result (printer_id, procedure, filament);
```

**That `CHECK` is the point of this table.** [spec.md §5.6](../spec.md#56-calibration-procedures)
requires every filament calibration to be scoped to a filament on a specific printer, and machine
calibrations to belong to the printer alone. Written as a constraint, a filament result with no
filament, or a machine result carrying one, cannot be stored at all. The rule is enforced by the
database rather than remembered by each caller.

`printer_id` is a column rather than a join through `session`, so a reassignment
([§4.5](#45-printer_binding)) cannot retroactively move a recorded result to a different machine.

### 4.12 `section_cache`

```sql
CREATE TABLE section_cache (
    content_hash TEXT NOT NULL PRIMARY KEY,
    result       TEXT NOT NULL,
    cached_at    TEXT NOT NULL
);
```

Keyed by the hash of a knowledge-base section's text, holding the extraction result for that
section — the printer fields when it is a printer, or a "not a printer" marker when it is not.
Unlike every other table, its primary key is a natural key (the hash) rather than a prefixed id,
because a cache entry is identified by its content and nothing else.

This is what makes [kb_ingestion.md §2.2](kb_ingestion.md#22-extraction-is-cached-by-content-hash)'s
claim true for **every** section, not only printers. `printer.kb_content_hash` records the hash of
a printer's current section, but a section classified as not-a-printer has no printer row to hold
its hash; without this table it would be re-sent to the extraction model on every document change.
The entry is written whatever the classification, so an unchanged section — printer or not — costs
no model call on re-ingest.

## 5. Artifact key layout

```
sessions/{session_id}/{artifact_id}{ext}
indexes/{artifact_id}/v{format_version}.idx
```

Two properties matter. Keys are **derived from identifiers, never from user-supplied filenames**,
which removes path traversal and collision as a class of problem. And they are **prefixed by
session**, so everything one session owns is one listing — which is what makes the future work of
session deletion tractable.

The `.idx` extension is a convention; the format is the file indexer's design, not the store's.

## 6. Connection handling

One SQLite database file. On open, in this order:

| Pragma | Value | Reason |
| --- | --- | --- |
| `journal_mode` | `WAL` | Readers do not block the writer, nor it them |
| `foreign_keys` | `ON` | Off by default; the schema depends on them |
| `busy_timeout` | Non-zero | Wait briefly on contention rather than failing instantly |
| `synchronous` | `NORMAL` | The safe pairing with WAL; `FULL` costs an fsync per commit |

`WAL` must be set before other connections open, and it persists in the file. `foreign_keys` and
`busy_timeout` are per-connection and must be set on every one.

## 7. Concurrency

**One writer, many readers.**

- A **single write connection**, owned by the store and serialised behind one lock. Every mutation
  goes through it. At one user's concurrency this costs nothing and removes write contention as a
  category.
- **Read connections** are separate and concurrent. WAL means they do not block the writer.
- **Writes never span an await on anything but the database.** A transaction is never held open
  across a model call, a printer request, or an approval wait. The approval gate blocks for as
  long as a human takes ([architecture.md §5.3](../architecture.md#53-printer-write-with-approval));
  a transaction held across that would stall every other session's writes. The proposal is
  committed, the wait happens outside any transaction, and the decision is a second commit.
- **SQLite calls run off the event loop.** The driver is blocking; calls execute in a thread so
  they cannot stall SSE streams or agent turns.

## 8. Migrations

Numbered files, `NNN_description.sql`, applied in ascending order inside a transaction, each
recording its version in `schema_version` as part of that same transaction. A partially applied
migration is therefore not a state that exists.

- **Startup applies pending migrations** before serving, and refuses to serve if any fails.
- **A database newer than the code is a fatal error**, not something to work around — it means a
  rollback happened without the data being considered.
- **Migrations are forward-only.** No down-migrations: with a single-user database and a backup
  taken before upgrade ([§9](#9-backup-and-restore)), restore is the rollback, and it is the one
  that actually works.
- **A migration that can lose data must say so** in a comment at the top of the file.

## 9. Backup and restore

- **Backup** uses SQLite's online backup, which produces a consistent copy while the system runs.
  Copying the file with `cp` is not equivalent and must not be documented as if it were: with WAL
  active it can capture a torn state.
- **Artifacts** are copied by a straightforward sync of the blob store, and the artifact copy runs
  **after** the database copy. In that order a restored pair can contain an artifact no row
  references — inert. The reverse order can produce a row pointing at an artifact that was never
  copied, which is a dangling reference.
- **Restore** is: stop the system, replace the database file and artifact tree, start. The
  migration step at startup then brings a restored older database up to the current schema.
- **Both halves must be restored together.** Neither is meaningful alone, and the documentation
  says so plainly.

## 10. Storage accounting

[spec.md §10](../spec.md#10-data-and-persistence-requirements) requires the system to be able to
state how much storage it is using, since nothing is ever deleted.

Reported as three figures: the database file size, `total_size` from the artifact store, and a
breakdown of artifact bytes by `kind`. The breakdown is the useful one — it is what shows that
G-code files and their indexes dominate, which is the growth worth watching.

## 11. Failure handling

| Failure | Behaviour |
| --- | --- |
| Database unreachable at startup | Fatal; refuse to serve rather than accept work that is lost |
| Migration fails | Fatal; the transaction rolls back and the version is unchanged |
| Database newer than the code | Fatal, with a message naming both versions |
| Artifact store unreachable at startup | Fatal for the same reason — half the record is missing |
| `put` fails mid-stream | No `artifact` row written; partial blob removed if the backend allows |
| Blob key missing at read | A specific error naming artifact and key, never an empty result |
| Constraint violation on write | A bug, not a condition; raised and logged with the statement |
| Process dies mid-turn | Tool calls with null `finished_at` marked interrupted, not assumed done |

The consistent rule: **a torn write is preferable to a silent one**. An artifact with no row wastes
bytes; a row with no artifact is a lie about what the system holds.

## 12. Testing

Per the project's engineering standards, every path is tested — the happy path and each failure
branch — and each test carries a one-line doc comment stating what it verifies.

- **Tests run against real SQLite**, on a temporary file per test. A fake SQL layer would be a
  re-implementation, not a mock: it would have to reproduce constraint enforcement, transaction
  semantics, and WAL behaviour accurately for the tests to mean anything, and would drift.
- **The artifact store is tested through its interface**, with the filesystem implementation
  against a temporary directory. The object-storage implementation is tested against the real
  service or not at all; a hand-written fake object store would have the same re-implementation
  problem.
- **Failures that cannot occur naturally** — a write failing mid-stream, a rename failing, a
  backup failing — are injected by exposing the relevant call as a module-level function variable
  that a test swaps out. Never by manipulating environment variables or global state.
- **Every failure path asserts its cleanup**, not just its error. The `put`-fails test asserts the
  partial blob is gone *and* that no row was written, since the guarantee is the pair.
- **Constraint tests are first-class.** The `procedure_result` `CHECK` and the `approval`
  uniqueness are load-bearing rules; each gets a test proving the invalid write is rejected.
- **Migrations are tested by applying them in order to an empty database**, and by applying the
  newest to a database built at the previous version.

## 13. Open questions

1. **Whether orphaned temporary objects are possible on the object-storage backend.**
   Correctness is not in question: [§11](#11-failure-handling) already guarantees a failed `put`
   writes no row, so the invariant that no row ever points at an incomplete blob holds on any
   backend. What is unresolved is only tidiness — whether the write-to-temporary-key-then-copy
   pattern can leave an orphaned temporary object behind, and whether a sweep is needed to
   collect them. This depends on the object store's own semantics and is resolved when that
   backend is implemented, not guessed at now.
2. **Whether a backup may run concurrently with active writes.** [§9](#9-backup-and-restore)
   fixes the *ordering* of the two halves so a torn pair is inert, but not whether the backup
   may run while the system is serving. SQLite's online backup handles a live database half; the
   artifact sync taken during in-flight `put`s is the unpinned part — a blob written after the
   database copy but during the artifact copy is inert (a blob with no row), but a blob written
   *before* the database copy and missed by an artifact copy already in progress would be a
   dangling reference. Resolve by either quiescing writes for the backup window or ordering the
   artifact sync to re-scan for anything added during the database copy.
3. **The `busy_timeout` value.** [§6](#6-connection-handling) requires it be non-zero but names
   no default. Under the single-writer discipline reader contention should be near-zero, so the
   value mostly bounds how long a read waits behind the writer's longest transaction. A concrete
   default is chosen in task T6.1 against measured transaction times.

**Resolved — not open:** whether `message.content` needs a full-text index. It does not, in this
version. Nothing in the spec searches across conversations — the session list is browsed by name,
printer, and recency ([spec.md §5.2](../spec.md#52-session-lifecycle)), not by content. Adding an
index later is a pure addition with no reshaping of existing data, so there is no cost to deferring
it and no uncertainty to record: it is a deliberate non-goal, not a question.

## 14. Implementation tasks

Each is a single reviewable commit. Marked complete as they land.

- [ ] **T4.1** Schema DDL as migration `001`, all tables, constraints, and indexes from
      [§4](#4-schema).
- [ ] **T4.2** Typed dataclasses for every entity, with the JSON columns modelled as structured
      types rather than raw strings.
- [ ] **T6.1** Connection handling: open, pragmas from [§6](#6-connection-handling), and the
      off-the-event-loop execution wrapper.
- [ ] **T7.1** Single-writer discipline: the write connection, its lock, and read connections.
- [ ] **T8.1** Migration runner: discovery, ordering, transactional application, version
      recording, and the newer-than-code check.
- [ ] **T3.1** `StructuredStore` for printers, config snapshots, and their queries.
- [ ] **T3.2** `StructuredStore` for sessions, bindings, and messages.
- [ ] **T3.3** `StructuredStore` for tool calls and approvals, including the interrupted-call
      sweep at startup.
- [ ] **T3.4** `StructuredStore` for artifacts, file indexes, and procedure results.
- [ ] **T3.5** `ArtifactStore` interface and the local filesystem implementation, streaming both
      ways, with the key layout from [§5](#5-artifact-key-layout).
- [ ] **T3.6** Object-storage implementation of `ArtifactStore`.
- [ ] **T10.1** Storage accounting: database size, artifact total, breakdown by kind.
- [ ] **T9.1** Backup and restore, including the ordering rule in [§9](#9-backup-and-restore) and
      the documented procedure.

Tasks are ordered by dependency: schema and connections before the stores that use them, the
filesystem artifact backend before the cloud one, and accounting and backup last since they read
everything else.
