# Capability gap analysis

The protocol's development backlog (s55). Each gap states the limitation, the
evidence that would justify building it, and how it would be verified - so the
next increment is driven by observed need rather than by what sounds
sophisticated (s52, s54).

Nothing here should be built simply because it is listed. The trigger column is
the point.

## Priority 1 - gaps the foundation already demonstrates

### Semantic retrieval
- **Limitation.** Retrieval is lexical. A query phrased in synonyms misses a
  relevant memory entirely.
- **Evidence it is needed.** `metrics.memory.empty_retrieval_rate` staying high
  while relevant memories demonstrably exist. This is measurable *today*.
- **Cost.** An embedding model or service; currently blocked by the
  standard-library-only constraint.
- **Verification.** A fixed query/memory set with known-correct answers,
  scored before and after. Adopt only if it beats the lexical baseline.
- **Interim mitigation.** Crude stemming, already implemented after a real miss
  ("run the tests" did not match "Test runner") - see the regression test.

### Memory consolidation
- **Limitation.** `assess()` prevents most duplication at write time, but
  nothing merges compatible knowledge or compresses repetitive episodes after
  the fact (s10).
- **Trigger.** Live memory count growing while `utilization` falls, or repeated
  near-duplicate assessments.
- **Verification.** Retrieval quality must not degrade after consolidation;
  provenance must survive every merge.

## Priority 2 - build when operation produces the evidence

### CLI command for objective status changes (s64)
- **Limitation.** `jarvis objective` only supports `add`, `list`, and `resolve`
  (an open question). There is no CLI command wired to `objectives.set_status`,
  so even the objective's own authority - the user - cannot mark an objective
  achieved or abandoned from the command line; only the Python API can.
- **Trigger met.** 22 September 2026, `obj_b7a913f7b04c`: the user asked to
  mark this objective achieved and there was no command to do it. Logged as
  task `tsk_c49895b2e063`, failure class `implementation_error`.
- **Design question this raises.** When Claude runs the CLI on the user's
  explicit chat instruction, is that the user's authority under s64, or the
  agent's own? If it is the user's, what should the command record to show
  the approval came from the user rather than from the agent's own
  judgement - a distinct actor, a `--confirmed-by-user` flag, something else
  captured in `authorized_by`? `set_status` already takes `authorized_by`
  and checks `actor != obj.authority`; wiring a CLI command in front of it
  without answering this risks reproducing exactly the fabricated-authorization
  failure the Authority model section of docs/ARCHITECTURE.md describes and
  fixed - recording a user approval that was actually the agent's inference.
- **Not building yet.** The design question above needs an answer first; a
  command that gets attribution wrong is worse than no command.

### Tool registry (s32/s33)
- **Trigger.** Repeated `invalid_tool_call` or `tool_failure` entries in
  `metrics.tasks.failure_classes`. The failure taxonomy exists precisely so
  this signal can accumulate.
- **Verification.** Failure rate for that class falls after registry-driven
  selection, with no regression elsewhere.

### Planning engine (s13/s14)
- **Trigger.** Repeated `planning_error` failures, or tasks routinely created in
  an order the dependency gate rejects.
- **Note.** Task decomposition and dependencies already exist; what is missing
  is automated decomposition and replanning. Build the smaller half first.

### Skill registry (s20/s21)
- **Trigger.** The same task shape recurring with a stable procedure and
  measurable benefit - explicitly *not* after one success (s21).
- **Verification.** The skill must outperform improvisation on the metric it
  claims to improve, measured against the recorded baseline.

### Experiment harness and baselines (s23/s24/s25)
- **Trigger.** The first proposed improvement that cannot be evaluated by
  inspection. `metrics(window_days=N)` is the baseline primitive; what is
  missing is isolation, hypothesis records and adoption decisions.
- **Constraint.** An experiment must never silently become production
  behaviour (s60).

## Priority 3 - only with demonstrated need

### Agent orchestration (s15-s19)
- **Trigger.** Work that genuinely needs isolation, parallelism or independent
  evaluation. The verification model already records *independence*, which is
  the honest first reason to want a second agent.
- **Warning.** s15 - do not create agents because more agents sounds advanced.

### Automation discovery (s39/s40) and proactive triggers (s41/s42)
- **Trigger.** A recurring workflow visible in the event log with stable shape,
  low risk and reversible effects.
- **Constraint.** Maturity ladder (s40): manual -> repeatable -> tested ->
  automated -> monitored. No jumping straight to autonomy.

## Added by the Task 1 acceptance audit

Non-critical improvements found during the audit and deliberately **not**
implemented, each with the trigger that would justify it.

### Pre-migration snapshot — IMPLEMENTED (Task 1 final hardening)
- Delivered in `jarvis/snapshot.py`; see `docs/SNAPSHOTS.md`.
- Verified snapshots via SQLite's online backup API, checked immediately with
  `integrity_check` and `foreign_key_check`, reported by `doctor` as a
  `recovery_point` health check.
- **Still open: automated restore / down-migrations.** Restore is a documented
  manual procedure, deliberately. A snapshot returns the database to a previous
  *state*, not a previous *schema* applied to today's data.
- **Trigger for automating restore.** A migration failure occurring often
  enough that the manual procedure becomes the risk, or an unattended context
  where no human is present to run it. Until then, code that can decide to
  discard the live database is a larger hazard than the problem it solves.

### Snapshot retention and pruning
- **Limitation.** Nothing deletes old snapshots; they are full copies, so disk
  use grows with every one taken.
- **Why not now.** Deciding what is safe to delete is a judgement, and the
  wrong default silently destroys the recovery point someone was relying on.
- **Trigger.** Snapshot storage becoming a real constraint, or more than a
  handful accumulating in normal use.

### CLI does not catch database-level errors
- **Limitation.** `main()` catches `ValueError`, `KeyError`, `RuntimeError`,
  `TransitionError` and `AuthorityError`; a `sqlite3.Error` escapes as a raw
  traceback.
- **Why not now.** It fails loudly and truthfully, which is the behaviour that
  matters. Only the presentation is poor.
- **Trigger.** A user-facing release, or the first report of confusion from a
  traceback.

### Abandoned sessions are never marked
- **Limitation.** A session whose process dies stays open forever; `boot()`
  handles it correctly but nothing records that it ended abnormally.
- **Trigger.** More than an occasional stale session, or a metric that needs
  accurate session durations.

### Multi-writer concurrency beyond first open
- **Limitation.** The first-open race is fixed and `busy_timeout` is set to
  5s, but sustained concurrent writing from several processes is untested
  beyond the audit's 8-process stress.
- **Trigger.** Two sessions routinely operating on one database - which agent
  orchestration would introduce.

### Structured episode recording
- **Limitation.** s7.2 lists the fields an episode should carry (approach,
  outcome, cause, correction, lesson). Today an episode is an `episodic`
  memory with a free-form `payload`; the structure is representable but not
  enforced or queryable.
- **Trigger.** Task 2, which is explicitly about persistent memory. Enforcing
  the shape before there are real episodes to shape it around would be guessing.

### Enforcement for the remaining permission categories
- **Limitation.** Only the objective-closing boundary is enforced.
- **Trigger.** The first capability that can take a consequential external
  action - a tool that writes outside the store, sends anything, or deletes.
  Recording is sufficient while every action is local and reversible.
- **Note.** This should be built together with actor authentication; gating on
  a self-declared identity provides confidence without security.

## Known limitations of what is built

Recorded so they are not mistaken for finished work:

1. **Sensitivity detection is a keyword heuristic.** It flags candidates for a
   human decision; it does not certify that content is safe to retain.
2. **Contradiction detection is lexical.** It catches polarity flips and
   differing numbers on the same subject, nothing subtler. A purely verbal
   contradiction with no digits and no negation (`eu-west` vs `us-east`) is
   missed, and because governance uses the same heuristic to waive its
   duplicate check, such a fact can still be skipped as a near-duplicate. This
   is the strongest argument for semantic retrieval above.
3. **Governance scores are caller-supplied judgements.** Durability, utility and
   decay come from the caller; only uniqueness and sensitivity are measured.
4. **`recovery_rate` counts a failed task later reaching completion.** It does
   not prove the recovery was causal rather than a lucky retry.
5. **No concurrency control.** Single-writer SQLite; two simultaneous sessions
   against one database are untested.
6. **Permission categories are recorded, not enforced - with one exception.**
   Only `objectives.set_status` is gated (protocol s64 reserves objectives to
   the user). Every other category is audited, not blocked. Attribution is now
   truthful, which is the part that had to be fixed; gating the rest waits for
   the trigger above.
7. **Actor identity is self-declared.** The log records who an action claims to
   be. Nothing authenticates it, so the audit trail is honest about *what*
   happened but trusts the caller about *who*.
8. **Migration is serialised, and recoverable but not reversible.** Concurrent
   first-open is safe, and a verified snapshot can be taken beforehand, but
   putting one back is a manual procedure and costs everything written since.
9. **The CLI cannot store sensitivity-flagged content at all.** `--force`
   overrides the value threshold only; the sensitivity gate needs
   `allow_sensitive=True`, which is API-only. So a legitimate note that merely
   *mentions* a credential ("the key lives in 1Password") cannot be added from
   the command line. Deliberate for V1 - the safe direction to fail - and the
   trigger to revisit is a real note being blocked in practice.
