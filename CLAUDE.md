# CLAUDE.md

Guidance for Claude Code (and other AI assistants) working in this repository.

## Repository status: greenfield

**This repository currently contains no application code.** As of the latest
update to this file, the entire tree is:

```
.
├── README.md    # single line: "# Autonomous-LLM"
└── CLAUDE.md    # this file
```

There is exactly one commit (`Initial commit`), no open issues, and no pull
requests. Do not assume any structure, framework, language, or tooling that is
not visible in the working tree — if a section below says "not yet chosen", it
means no decision has been recorded anywhere in the repo, not that it is
documented elsewhere.

Repository facts:

| | |
|---|---|
| Remote | `https://github.com/mamourette-code/Autonomous-LLM` |
| Default branch | `main` |
| Visibility | public |
| Issues / Projects / Wiki | enabled |
| CI | none configured (no `.github/workflows`) |

## What is not established yet

None of the following exist; there is nothing to read, run, or imitate:

- **Language / runtime** — no `package.json`, `pyproject.toml`, `go.mod`,
  `Cargo.toml`, or equivalent.
- **Build, run, and test commands** — nothing to invoke.
- **Test framework and layout** — no tests, no test directory.
- **Lint / format configuration** — no linter, formatter, or editorconfig.
- **Dependency management and lockfiles.**
- **CI workflows, pre-commit hooks, or release process.**
- **Architecture** — no modules, entry points, or boundaries to describe.

The project name suggests work on autonomous LLM agents, but nothing in the
repository states its goals, scope, or intended design. **Do not infer a
product direction from the name.** If a task requires knowing what this project
is meant to be, ask the user rather than guessing.

## Working here

### Before writing code

1. Re-read the working tree first (`git ls-files`). This file describes the
   repository as of the last time someone updated it; the tree is the truth.
2. If the task is open-ended ("build the agent loop", "add the API"), confirm
   the stack and scope with the user before scaffolding — the first commit that
   adds a language and toolchain sets conventions everyone else inherits, and
   that choice is the user's to make, not a default to pick silently.
3. Once a stack exists, follow it. Match the surrounding code's naming,
   structure, and comment density rather than importing conventions from
   elsewhere.

### Update this file as the repository grows

Whenever a change establishes something durable, record it here in the same
commit. Specifically, replace the "What is not established yet" section with
real content as soon as each item exists:

- **Commands** — the exact invocations for install, build, run, test (full
  suite *and* a single test), lint, and format. These are the highest-value
  entries in a CLAUDE.md; write the literal command line, not a description.
- **Layout** — top-level directories and what belongs in each.
- **Architecture** — entry points, module boundaries, data flow, and any
  non-obvious design decisions or constraints a newcomer would otherwise
  violate.
- **Conventions** — anything a contributor must do that the tooling does not
  enforce automatically.
- **Environment** — required environment variables and services. Document
  *names and purposes only*; never commit secrets, API keys, or `.env` files.
  Add a `.env.example` with placeholder values instead, and add `.env` to
  `.gitignore` as soon as one is introduced.

Keep this file short and factual. It is loaded into context on every session,
so it should hold what is not obvious from reading the code — not a
restatement of it. Delete entries that go stale; a wrong CLAUDE.md is worse
than a thin one.

## Git conventions

- **Never commit directly to `main`.** Work on a feature branch and open a PR.
- Claude Code sessions use branches named `claude/<short-description>-<suffix>`
  (e.g. `claude/claude-md-docs-iyg313`). Human contributors are free to use
  their own naming.
- Push with `git push -u origin <branch-name>`.
- Write commit messages in the imperative mood with a short subject line, and a
  body explaining *why* when the change is not self-evident.
- **Do not open a pull request unless the user explicitly asks for one.**
- Do not rewrite history (rebase, amend, force-push) on a branch someone else
  may have checked out.

## LLM / API work

When this project starts calling model APIs:

- Default to the current Claude models and pass model IDs explicitly rather
  than relying on provider defaults.
- Read API keys from the environment. Never hardcode credentials or check them
  into the repository.
- Record the chosen provider, SDK, and model IDs in this file once they are
  settled, so later sessions do not have to rediscover them.
