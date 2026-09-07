# Autonomous-LLM — JARVIS foundation layer

A persistent, inspectable substrate around Claude: state that survives the
context window, an audit log that cannot flatter itself, an explicit task and
verification model, and memory that is governed rather than accumulated.

Standard library only — no dependencies to install.

## Quick start

```bash
python3 -m jarvis doctor                     # what this system can and cannot do
python3 -m jarvis boot --focus "the thing you are about to work on"
python3 -m jarvis snapshot create --reason "before a migration"
python3 -m jarvis task add --title "write the parser"
python3 -m jarvis close ses_... --summary "what happened"
```

State lives in `$JARVIS_HOME/jarvis.db` (default `./.jarvis/jarvis.db`).
Every command takes `--json` for machine use and `--db` for an isolated store.

## What it does

**Session continuity (s47/s48).** `boot` returns an oriented briefing — open
objectives and their unresolved questions, actionable tasks, blocked work,
recent failures, completions that were never verified, stale memories,
unresolved contradictions, health. With `--focus` it retrieves the relevant
memories for that focus instead of loading everything. `close` reports what
actually happened and what the next session inherits.

**Objectives (s5).** A request becomes an explicit desired outcome with
constraints, assumptions, verification criteria and *open questions*. An
objective with an unresolved question, or with no way to falsify success, is
not ready — and says which.

**Tasks and verification (s6/s29/s30).** Eight states with an enforced
transition table. `COMPLETED` is not `VERIFIED`: reaching `VERIFIED` requires a
recorded method and evidence, and whether the check was *independent* is derived
from the audit log rather than asserted. A failure cannot be recorded without a
class from the taxonomy, so recovery can be causal instead of a retry loop.

**Memory (s7–s12, s49).** Six kinds crossed with seven epistemic classes — a
hypothesis is never stored as if it were sourced knowledge. `assess()` scores a
candidate on durability, utility, uniqueness, confidence, sensitivity, context
and decay, and stores only when expected benefit exceeds cost and risk.
Possibly sensitive content is referred to the user. Volatile facts carry a
review date. Corrections supersede rather than delete.

**Retrieval (s9/s50).** Ranked by lexical relevance, entity and project match,
recency, epistemic trust and prior usefulness — with the component scores
returned, so a bad ranking can be diagnosed. Contradictions are surfaced and
persisted as unresolved, both sides kept, confidence reduced. Nothing is
silently merged.

**Observability and self-diagnosis (s35/s43/s44/s45/s57).** Every mutation is
audited, and a state change commits together with its audit event or not at
all. Metrics are derived from the log at read time, so they cannot drift. An
unmeasured rate reports `n/a`, never `0%`. `doctor` reports health, metrics,
open work — and the capabilities this system does **not** have.

**Recovery points.** `jarvis snapshot create` takes a verified, self-contained
copy of the database using SQLite's online backup API — safe to run while the
database is in use, unlike a file copy. Each snapshot is checked immediately
with `integrity_check` and `foreign_key_check` by opening it on its own, and
carries a JSON sidecar recording its schema version, per-table row counts and
provenance. `doctor` warns when a populated database has no verified snapshot
for its current schema. Restore is a documented manual procedure, not an
automated rollback — see [`docs/SNAPSHOTS.md`](docs/SNAPSHOTS.md).

**Authority (s34/s64).** Permission categories and authorizations are recorded.
One boundary is *enforced*: only an objective's authority may declare it
achieved or abandoned, and a refused attempt is recorded as `denied`.
Attribution is truthful — `authorized_by` names a real authorization by the
acting party, and `actor` must be stated explicitly rather than defaulting to
the user. Everything else is audited, not gated, and `doctor` says so.

## What it deliberately does not do

No planner, no agents, no skill registry, no tool registry, no experiment
harness, no automation, no proactive behaviour. The environment was empty, so
there is no operational evidence yet showing what shape those need, and each
carries permanent maintenance cost (s53/s54). `docs/BACKLOG.md` records the
evidence that would justify building each one — several of which this layer can
now actually measure.

## Tests

```bash
python3 -m unittest discover -s tests -v   # 154 tests
```

`tests/test_audit_regressions.py` holds the regressions from the Task 1
acceptance audit — each one reproduces a defect the original suite passed over.

## Documentation

- [`docs/CAPABILITY_ASSESSMENT.md`](docs/CAPABILITY_ASSESSMENT.md) — what was
  found in the environment and why this was built and not more
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — subsystem map, enforced
  invariants, data model
- [`docs/BACKLOG.md`](docs/BACKLOG.md) — capability gaps, their triggers, and
  the known limitations of what exists
- [`docs/SNAPSHOTS.md`](docs/SNAPSHOTS.md) — how snapshots are made, and the
  recovery procedure
