# Capability assessment

Performed before any implementation, as the protocol's primary development
directive requires (s1). Recorded here so the next session inherits the finding
instead of re-deriving it.

## What was inspected

| Question | Method | Finding |
|---|---|---|
| What exists? | `git log`, full file tree | One commit, one 16-byte `README.md`. Nothing else. |
| Existing agents, MCP servers, scripts, databases? | file tree, repo search | None in the repository. |
| Runtime? | `python3 --version`, `node --version` | Python 3.11.15, Node 22.22.2 |
| Third-party Python packages? | `pip list` | Standard library only. No pytest, no ORM, no vector store. |
| Branch state | `git rev-parse` | `main` and the feature branch both at the initial commit. |

**Conclusion: the environment is empty.** There was nothing to reuse, refactor
or retain, so the assessment reduces to a single question: what is the
highest-value capability that does not exist?

## Constraints this imposes

1. **Standard library only.** No dependency may be assumed available. This
   forces SQLite for storage and `unittest` for tests, and rules out embedding
   based retrieval for now. That is a real limitation, recorded in the backlog
   rather than papered over.
2. **The container is ephemeral.** Anything not committed is lost, which is
   precisely the continuity problem the protocol describes (s46).

## What was built, and why that and not more

The protocol's own priority order (s56) puts reliable execution, observability,
persistent state, memory retrieval, task management and verification ahead of
skills, agents, learning and automation. Against an empty environment those
first six are exactly the missing foundation, so that is what this change
implements - and it stops there.

Planning, agents, skills, tool registries, experimentation and automation are
deliberately **not** implemented. Building them now would violate s53 and s54:
there is no operational evidence yet showing what shape they need, and each one
carries permanent maintenance cost. They are recorded in `BACKLOG.md` with the
evidence that would justify building each.

The system reports this distinction about itself at runtime: `jarvis doctor`
lists both what is implemented and what is not, and a test asserts that every
claimed capability resolves to real code (s57).
