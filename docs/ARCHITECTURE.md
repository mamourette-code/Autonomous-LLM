# Architecture

The foundation layer of JARVIS: persistent state, observability, an explicit
task and verification model, and governed memory with ranked retrieval.

Claude is the reasoning engine. This package is the part of the system that
does *not* live in the context window - the part that still exists after the
conversation ends.

## Subsystem map

```
                       jarvis.cli  /  jarvis.session
                       (human control, boot & close)
                                   |
   +-------------------+-----------+-----------+-------------------+
   |                   |                       |                   |
objectives           tasks                  memory            diagnostics
(s5: what the      (s6/s29/s30:          (s7/s8/s9/s49:     (s44/s45/s57:
 user wants)        state machine,         governance,        metrics,
                    verification,          retrieval,         health,
                    failure classes)       contradictions)    self-report)
   |                   |                       |                   |
   +-------------------+-----------+-----------+-------------------+
                                   |
                              jarvis.events
                        (s35/s43: audit + traces)
                                   |
                              jarvis.store
                       (SQLite, schema, migrations)
```

Every subsystem writes through `events`, and every metric is derived from that
log at read time. Nothing keeps its own counters, so a metric cannot drift away
from what actually happened.

## The invariants that carry the weight

Most of this package is bookkeeping. These few rules are the part that changes
behaviour, and each is enforced in code and covered by a test:

| Invariant | Where | Why |
|---|---|---|
| `COMPLETED` and `VERIFIED` are different states, and `VERIFIED` is unreachable except through `verify()` with a method and evidence | `tasks.transition`, `tasks.verify` | s6 - claiming completion is not evidence of correctness |
| Independence of a verification is *derived* from the audit log, never claimed | `tasks._completer` | s29 - re-running the same reasoning is not independent verification |
| A failure cannot be recorded without a class from the taxonomy | `tasks.transition` | s30/s31 - recovery must be causal, not a retry loop |
| A task cannot start while a dependency is unmet | `tasks.unmet_dependencies` | s13 - ordering is checked, not assumed |
| Nothing is remembered reflexively; `assess()` must find expected value above threshold | `memory.assess` | s8 - storage has a cost and a risk |
| Possibly sensitive content is referred to the user, never auto-stored | `memory.sensitivity` | s8/s64 - retention is a human decision |
| `sourced_knowledge` without a source is rejected | `memory.remember` | s12 - provenance is not optional |
| Contradictions are surfaced and persisted as unresolved; both sides survive with reduced confidence | `retrieval.detect_conflicts` | s50 - never silently merge or pick a winner |
| A candidate that contradicts stored knowledge is exempt from both the uniqueness penalty and the value threshold | `memory.assess` | s50 - dropping the weaker side is silently picking a winner |
| Heavy-but-partial overlap returns a supersede candidate instead of vanishing | `memory.assess` | s10 - a revision is not a duplicate |
| Corrections supersede rather than delete | `memory.supersede` | s12 - "what did we believe, and when" stays answerable |
| An unmeasured rate reports `None`, not `0%` | `diagnostics._ratio` | s63 - no evidence is not the same as a zero score |
| Claimed capabilities must resolve to real code | `tests/test_diagnostics.py` | s57 - do not claim what is not verified |

## Data model

Eight tables, one schema version, forward-only migrations in `store._MIGRATIONS`.

- `objectives` - desired outcome, constraints, assumptions, **open questions**,
  verification criteria. An objective with open questions or no verification
  criteria is not "ready" and says why.
- `tasks` + `task_deps` - the eight protocol states, dependencies, expected vs
  actual output, failure class.
- `verifications` - method, evidence, verifier, derived independence, pass/fail.
- `memories` - six kinds (episodic, semantic, procedural, user, project,
  decision) crossed with seven epistemic classes (s11), plus confidence,
  provenance, review date and use count.
- `memory_conflicts` - detected contradictions and how they were resolved.
- `events` - the audit log: actor, target, outcome, permission category,
  duration, session.
- `sessions` - boot/close records that give the next session its starting point.

## Retrieval, and what it is not

Ranking combines weighted lexical overlap (idf over the corpus, crude stemming),
entity and project match, recency, epistemic trust and prior usefulness. Each
hit returns its component scores, so a bad ranking can be diagnosed instead of
guessed at.

This is lexical retrieval. It has no semantic understanding: a query phrased
entirely in synonyms will miss. Embeddings are unavailable in this environment
(standard library only) and are the first entry in the backlog.

Contradiction detection is likewise a heuristic - same-subject pairs differing
in polarity or in a stated number. A looser "the wording differs" rule was tried
and removed: it flagged two phrasings of the *same* fact as a conflict, and
false conflicts train the reader to ignore real ones. So it will miss
contradictions expressed purely in words (`eu-west` vs `us-east` with no digits
present). That is why its output is a flag for resolution and never an automatic
merge or deletion.

The same heuristic is used at write time. Governance would otherwise suppress
disagreement twice over - a contradicting fact looks like a near-duplicate, and
a low-confidence contradiction falls under the value threshold - and in both
cases the older belief would quietly win. Both exemptions exist for that reason,
and both are covered by regression tests derived from the actual failures.

## What is deliberately absent

No planner, no agents, no skill registry, no tool registry, no experiment
harness, no automation, no proactive triggers. `jarvis doctor` says so out loud.
See `BACKLOG.md` for the evidence that would justify building each one.
