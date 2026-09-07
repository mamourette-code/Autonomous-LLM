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
6. **Permission categories are recorded, not enforced.** The audit log captures
   which category an action needed and who authorized it; nothing yet blocks an
   unauthorized action. That gap is deliberate - enforcement without a real
   authority model would be theatre - and is the next security-relevant step.
