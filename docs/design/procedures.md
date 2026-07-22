# Procedures — module design

This module holds the calibration catalog: what each procedure is for, what it needs before it can
start, what it does, and how its result is recorded and scoped.

Requirements: [spec.md §5.6](../spec.md#56-calibration-procedures). Referenced by
[architecture.md §4](../architecture.md#4-data-model) for the result shape.

## 1. Scope

**In scope:** the catalog format, the six procedures, precondition checking, and how results are
recorded with their scope.

**Out of scope:** running a procedure. There is no procedure engine — the agent runs procedures by
reading the catalog and using the ordinary tools, and every command it issues goes through the
same approval gate as anything else ([orchestration.md §6](orchestration.md#6-the-approval-gate)).
This module supplies knowledge and one constraint; it does not supply control flow.

## 2. Design decisions

### 2.1 Procedures are data, not code

Each procedure is a structured document loaded at startup and placed in the system prompt. Not a
code module implementing an interface.

The reason is that a procedure is knowledge, not behaviour. Its content is purpose, preconditions,
steps, commands, and how to read the result — all of which is prose and values. Encoding that as
code would mean a release to fix a wrong temperature range or add a step, and it would put the
interesting content inside functions where it cannot be read as a whole.

The catalog being data also means the model reads the *whole* procedure and can adapt it. A
procedure that were code would run as written, which is the wrong shape for a step that depends on
what a photo shows.

All six documents are placed in full in the cached stable prefix
([orchestration.md §4.1](orchestration.md#41-system-prompt-assembly)), so their cost is paid once
and amortised across every session rather than re-sent each time. Six calibration documents are
kilobytes; loading only the relevant one on demand would forfeit the cross-session cache for a
saving that does not matter at this size. Turning each procedure into a skill the agent loads only
when relevant is future work for when the catalog grows well beyond six
([spec.md §14](../spec.md#14-future-work)).

### 2.2 There is no procedure state machine

[decisions.md](../decisions.md) settled that a procedure run lives in the conversation: no run
object, no tracked state, no polling, no notifications. A test print takes hours and the user
closes the browser; resumption works because the conversation is complete and durable
([spec.md §5.6](../spec.md#56-calibration-procedures)).

That places the requirement on the *content* instead. Every procedure document must instruct the
agent to leave the transcript unambiguous when read hours later — what was started, on which
printer and filament, what the user was asked to do, and what evidence is expected next. That
instruction is part of the catalog format, not an afterthought.

### 2.3 Scope is enforced by the database, not the document

A filament calibration must carry a filament and a machine calibration must not
([spec.md §5.6](../spec.md#56-calibration-procedures)). That rule lives as a `CHECK` constraint in
`procedure_result` ([design/store.md §4.11](store.md#411-procedure_result)), so a wrongly-scoped
result cannot be stored at all.

The catalog declares each procedure's scope; the schema enforces it. Declaring it in one place and
enforcing it in another is deliberate — a document can be edited carelessly, a constraint cannot.

## 3. Catalog format

One document per procedure, with these fields:

| Field | Contents |
| --- | --- |
| `id` | Stable identifier, matching the `procedure` values in the schema |
| `name` | Human-readable |
| `scope` | `printer` or `printer_and_filament` |
| `purpose` | What it establishes and when it is worth doing |
| `preconditions` | Machine-checkable conditions, each with what it disables and how to fix it |
| `hardware_requirements` | What must exist on the printer, checked against its configuration |
| `steps` | Ordered; each with intent, commands, and what the user must do physically |
| `test_source` | Where the test object or test G-code comes from, when the procedure needs one |
| `evidence` | What to collect: a value, a graph, a photograph of a test print |
| `interpretation` | How to read the evidence, including what a bad result looks like |
| `results` | The values it produces and their units |
| `records` | What to write into the session and what to suggest for the KB document |

`preconditions` and `hardware_requirements` are separated because they fail differently: a
precondition is a state the user can change now ("the printer is currently printing"), while a
hardware requirement is a fact about the machine ("no accelerometer is configured") that means the
procedure is not available on it at all.

### 3.1 Test sources

**No test model is shipped in the repository**, which sidesteps the licensing question bundling
STLs would raise. `test_source` names where a procedure's test comes from, and it is one of three
kinds:

- **The slicer's own calibration generator.** OrcaSlicer has a built-in calibration menu —
  temperature tower, flow, pressure advance, retraction — so for those the user generates the test
  in the slicer they already use.
- **A named external G-code generator**, linked — for pressure advance, for example, Ellis's
  [Pressure/Linear Advance tool](https://ellis3dp.com/Pressure_Linear_Advance_Tool/).
- **A link to a community model**, on a site such as Printables, for a physical test object not
  covered by the above.

Procedures that use a Klipper macro rather than a printed test — `input_shaper`, `pid_tune` — have
no `test_source`.

## 4. The catalog

Six procedures, per [spec.md §5.6](../spec.md#56-calibration-procedures):

| Procedure | Scope | Establishes | Notable requirement |
| --- | --- | --- | --- |
| `input_shaper` | Printer | Resonance frequencies and shaper choice per axis | An accelerometer |
| `pid_tune` | Printer | Bed and hotend heater PID values | — |
| `first_layer` | Printer + filament | Nozzle-to-bed distance and a clean first layer | — |
| `pressure_advance_flow` | Printer + filament | Pressure advance and flow ratio | A test print |
| `temperature` | Printer + filament | Nozzle and bed temperatures | A test print |
| `stringing_retraction` | Printer + filament | Retraction length and speed, travel | A test print |

### 4.1 First layer is the mixed case

`first_layer` is scoped `printer_and_filament`, but what it establishes splits: the mechanical
Z-offset belongs to the printer, while first-layer temperature, speed, squish, and cooling are per
filament ([spec.md §5.6](../spec.md#56-calibration-procedures)).

The two halves are recorded in different places. The mechanical Z-offset is a saved-config value —
in Klipper `[probe] z_offset`, set with `SAVE_CONFIG` — so it lives in the printer and is captured
by the configuration snapshot ([printer_access.md §4](printer_access.md#4-the-three-tiers)), not
written to `procedure_result`. The `procedure_result` row carries only the filament-scoped values,
which is why its `CHECK` requiring a filament
([§2.3](#23-scope-is-enforced-by-the-database-not-the-document)) is correct for `first_layer`. The
per-filament squish the user applies is a slicer setting, consistent with the system owning no
filament profiles.

The document must still state the split explicitly, because it is the one place where a careless
reading would carry a filament-specific value across to another material.

### 4.2 The example printers exercise both failure kinds

[examples/printer_definition.md](../examples/printer_definition.md)'s two printers are deliberately
dissimilar, and between them they hit both precondition kinds: one has an accelerometer and one
does not, so `input_shaper` is unavailable on the second as a *hardware* fact, not a state the user
can fix by waiting.

## 5. Precondition checking

Before a procedure starts, its preconditions and hardware requirements are checked against the
printer's configuration and live state. Failures are reported **before** starting, naming what is
missing and what would fix it — never discovered partway through
([spec.md §5.6](../spec.md#56-calibration-procedures)).

| Condition | Checked against |
| --- | --- |
| Hardware present | Configuration snapshot |
| Printer idle | Live status |
| Printer homed | Live position state |
| Temperatures reachable | Configured limits |
| Required material loaded | Asked, not inferred |

A hardware requirement failure makes the procedure **unavailable** on that printer and the system
says so plainly rather than offering it and failing later.

The live-state checks — idle, homed, temperatures — require the printer reachable, and a procedure
cannot run without it in any case. An unreachable printer therefore blocks the procedure at
precondition time, reported as needing the printer, rather than the live checks being treated as
indeterminate and the procedure started anyway.

## 6. Recommending a procedure

The system may recommend a procedure based on the printer's recorded calibration status and the
problem under discussion ([spec.md §5.6](../spec.md#56-calibration-procedures)). Calibration status
comes from the KB document's prose, which is where the user records it — the example document says
things like "input shaping last run 2025-11, before the toolhead was rebuilt."

That is a judgement the model makes from the prose plus the conversation, not a rule this module
encodes. What this module supplies is the catalog entry that makes the recommendation specific:
what it establishes, what it needs, and what it costs.

## 7. Recording results

On completion:

- A `procedure_result` row, scoped per
  [§2.3](#23-scope-is-enforced-by-the-database-not-the-document), with the values, the printer,
  the filament when applicable, and references to the evidence artifacts.
- **For `first_layer`, the mechanical Z-offset is not in that row.** It is a saved-config value
  captured by the configuration snapshot ([§4.1](#41-first-layer-is-the-mixed-case)); the row holds
  only the filament-scoped first-layer values.
- **A suggested edit for the KB document**, as text for the user to apply. The system never writes
  to that document ([spec.md §5.1](../spec.md#51-printer-management)); the calibration-status line
  it would update is exactly the prose the user maintains.
- **Recommendations naming both filament and printer**, per
  [spec.md §5.8](../spec.md#58-output-and-recommendations) — since the system owns no filament
  profiles, the user is the one filing the value away, and a value recorded without knowing which
  machine produced it is worse than no value.

### 7.1 Values from another printer are a starting point, never a result

When a value exists for the same filament on a different printer, the system may mention it as a
starting point to be re-tuned, labelled as such. It must never be presented as this printer's value
([spec.md §5.6](../spec.md#56-calibration-procedures)).

## 8. Failure handling

| Failure | Behaviour |
| --- | --- |
| Catalog document malformed at startup | Fatal — a broken catalog is a broken system prompt |
| Unknown `id` or `scope` | Fatal at load; the schema's constraint would reject the results anyway |
| Precondition fails | Reported before starting, with what is missing and how to fix it |
| Hardware requirement fails | Procedure reported unavailable on that printer |
| Printer unreachable before start | Blocked at precondition; reported as needing the printer |
| Printer unreachable mid-procedure | Reported plainly; no assumption about the last command |
| User abandons a procedure | Nothing to clean up — no run state; the transcript records it |
| Evidence photo unusable | The photo-quality rules apply: say so and ask for a better one |

## 9. Testing

- **Every catalog document is validated at load**, and a test asserts each of the six parses,
  declares a valid scope, and names only procedure identifiers the schema accepts.
- **Scope declarations are tested against the schema constraint** — a test proves the catalog's
  declared scope for each procedure matches what `procedure_result` will accept, so the two
  cannot drift apart silently.
- **Precondition checking is tested per procedure**, with both a passing and a failing case, a
  test that a hardware failure reports unavailable rather than not-now, and a test that an
  unreachable printer blocks at precondition time rather than starting on indeterminate live state.
- **The example printers are the fixtures**, so the accelerometer-present and accelerometer-absent
  cases are both exercised.
- **Result recording is tested for both scopes**, including that a filament result without a
  filament is rejected and a machine result with one is rejected.
- **`first_layer` result routing is tested**: the row records only the filament-scoped values, and
  the mechanical Z-offset is not written to `procedure_result`.
- **Test sources are validated at load**: a procedure needing a printed test declares a
  `test_source`, and the macro-based procedures declare none; no `test_source` references a model
  shipped in the repository.
- **The suggested-edit output is tested** for containing the printer, the procedure, the values,
  and the date — and for not being applied to any file.

## 10. Open questions

1. **Whether the catalog should carry expected value ranges.** A pressure advance of 2.0 is almost
   certainly wrong, and a range would let the system say so. But ranges vary by extruder and
   material enough that a wrong range would be worse than none — flagging a correct-but-unusual
   value as suspect teaches the user to distrust the check. If ranges are added, the likely shape is
   to split them: wide physical-impossibility bounds (a pressure advance of 2.0, a nozzle at 400°C)
   are material-agnostic and almost never wrong to flag, while tight per-setup expected ranges are
   the dangerous part the warning is about. Deciding needs real calibration data across the two
   example printers' different extruder types.

## 11. Implementation tasks

- [x] **T3.1** Catalog format, loader, and startup validation.
- [x] **T3.2** Scope-declaration cross-check against the schema constraint.
- [x] **T4.1** `input_shaper` and `pid_tune` documents (printer-scoped).
- [x] **T4.2** `first_layer` document, including the mixed-scope split and routing the mechanical
      Z-offset to saved config rather than `procedure_result`.
- [x] **T4.3** `pressure_advance_flow`, `temperature`, and `stringing_retraction` documents.
- [x] **T5.1** Precondition and hardware-requirement checking against configuration and live state.
- [x] **T7.1** Result recording with scope enforcement and evidence references.
- [x] **T7.2** Suggested KB-document edit generation.
- [x] **T3.3** Catalog inclusion in system prompt assembly (with
      [orchestration.md](orchestration.md)).
