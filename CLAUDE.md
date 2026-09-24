# CLAUDE.md

Guidance for Claude Code sessions working in this repository.

## Using JARVIS

A `jarvis` session boots automatically (see `.claude/hooks/`). Read the boot summary line it prints; if it shows ready tasks or unverified completions, mention them to the user before starting new work.

- **Record a task** when real work starts (not every message), linked to an objective if one fits: `python3 -m jarvis task add --title "..." --expected "..." [--objective <obj_id>] [--depends-on <task_id>]`, then `python3 -m jarvis task start --id <id>`.
- **Complete and verify separately** — completed is not verified: `python3 -m jarvis task complete --id <id> --actual "..."`, then `python3 -m jarvis task verify --id <id> --method "..." --evidence "..."` (`--reject` records a failed verification). Verifying your own work is recorded as non-independent — that's fine, JARVIS derives independence from the audit log itself, but never describe a self-verification as independent.
- **Skill runs get tracked too**: when a session uses a skill from `.claude/skills/`, record the run as a task titled `skill:<name>` (e.g. `skill:pr-workflow`), and when you verify it, note in `--evidence` whether the output was accepted as-is or needed correction, and what kind.
- **Record failures** with a class from the taxonomy, never a bare note: `python3 -m jarvis task fail --id <id> --failure-class <class> --note "..."`. Classes: `incorrect_assumption`, `missing_information`, `invalid_tool_call`, `tool_failure`, `implementation_error`, `planning_error`, `retrieval_failure`, `memory_failure`, `verification_failure`, `permission_failure`, `environmental_failure`, `external_dependency_failure`.
- **Remember** only what the user stated or what's verified, always with provenance in `--source`. `python3 -m jarvis memory add --kind <kind> --title "..." --body "..." --source-class <class> --source "..." --confidence 0.N` already runs the assess gate first and only stores if it clears the value threshold (`--force` overrides and is recorded) — never store your own speculation. Preview without storing: `python3 -m jarvis memory assess --kind ... --title ... --body ...`.
- **Objectives are the user's** (protocol s64): propose a title, outcome and verification criteria in chat, and only run `python3 -m jarvis objective add --title "..." --outcome "..." --verify "..."` once the user agrees — don't create one unprompted. Never declare an objective achieved yourself: there is no CLI command for it, and the underlying code rejects any actor but the objective's own authority.

Check in anytime with `python3 -m jarvis doctor` (health + open work) or `python3 -m jarvis task show --id <id>` (full history + verifications).
