-- Migration 001: initial schema.
--
-- Every table, constraint, and index from store.md §4. Applied inside a transaction by the
-- migration runner, which also records the version in schema_version as part of the same
-- transaction (store.md §8). This migration creates data structures only; it cannot lose data.

CREATE TABLE schema_version (
    version    INTEGER NOT NULL PRIMARY KEY,
    applied_at TEXT    NOT NULL
);

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

CREATE TABLE config_snapshot (
    id            TEXT NOT NULL PRIMARY KEY,
    printer_id    TEXT NOT NULL REFERENCES printer(id),
    source        TEXT NOT NULL CHECK (source IN ('files', 'moonraker')),
    captured_at   TEXT NOT NULL,
    contents      TEXT NOT NULL,
    discrepancies TEXT
);

CREATE INDEX idx_config_snapshot_printer ON config_snapshot (printer_id, captured_at DESC);

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

CREATE TABLE printer_binding (
    id         TEXT NOT NULL PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES session(id),
    printer_id TEXT NOT NULL REFERENCES printer(id),
    bound_at   TEXT NOT NULL,
    reason     TEXT NOT NULL CHECK (reason IN ('detected', 'chosen', 'reassigned'))
);

CREATE INDEX idx_printer_binding_session ON printer_binding (session_id, bound_at);

CREATE TABLE message (
    id         TEXT    NOT NULL PRIMARY KEY,
    session_id TEXT    NOT NULL REFERENCES session(id),
    seq        INTEGER NOT NULL,
    role       TEXT    NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL,
    UNIQUE (session_id, seq)
);

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

CREATE TABLE approval (
    id               TEXT NOT NULL PRIMARY KEY,
    tool_call_id     TEXT NOT NULL UNIQUE REFERENCES tool_call(id),
    proposed_command TEXT NOT NULL,
    danger_flags     TEXT,
    decision         TEXT NOT NULL CHECK (decision IN ('approved', 'rejected', 'timed_out')),
    decided_by       TEXT NOT NULL,
    decided_at       TEXT NOT NULL
);

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

CREATE TABLE file_index (
    id             TEXT    NOT NULL PRIMARY KEY,
    artifact_id    TEXT    NOT NULL UNIQUE REFERENCES artifact(id),
    kind           TEXT    NOT NULL CHECK (kind IN ('project', 'gcode')),
    blob_key       TEXT    NOT NULL UNIQUE,
    format_version INTEGER NOT NULL,
    built_at       TEXT    NOT NULL
);

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

CREATE TABLE section_cache (
    content_hash TEXT NOT NULL PRIMARY KEY,
    result       TEXT NOT NULL,
    cached_at    TEXT NOT NULL
);
