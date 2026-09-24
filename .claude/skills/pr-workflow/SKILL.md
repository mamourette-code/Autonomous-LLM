---
name: pr-workflow
description: Ships an already-made change as a pull request following this repo's conventions - creates a claude/* branch off the default branch, commits and pushes only the agreed file scope, opens a PR (using a repo PR template if one exists), verifies the PR diff before merging, merges, and reports the branch name to delete. Use this skill once code/doc edits are finished and ready to go out - not for writing the change itself.
---

# PR workflow

Given: a short description of the change (for the commit message and PR title/body) and the file scope it must be limited to (the exact paths the diff should touch).

1. Confirm both inputs are known; ask if either is missing rather than guessing scope.
2. `git status` - check nothing outside the agreed scope is staged or modified. If something unexpected is there, stop and flag it rather than silently including or discarding it.
3. `git fetch origin <default-branch>` and `git checkout -b claude/<short-description> origin/<default-branch>` - always branch fresh off the remote default branch, never off local state that might carry unrelated work.
4. Stage only the agreed-scope files by explicit path (never `git add -A`/`.`), commit with a message explaining why, `git push -u origin <branch>`.
5. Check for a PR template (`.github/pull_request_template.md`, `.github/PULL_REQUEST_TEMPLATE.md`, root `PULL_REQUEST_TEMPLATE.md`, `docs/PULL_REQUEST_TEMPLATE.md`); use it to structure the PR body if one exists. Never push directly to the default branch.
6. Open the PR.
7. Fetch the PR's file list and confirm it is exactly the agreed scope - no more, no fewer files. If it doesn't match, do NOT fix it yourself and do NOT merge: report which files are extra or missing and wait for the user.
8. Merge only once the diff is confirmed and (unless the original request already said to merge) you have explicit go-ahead.
9. Report the branch name back so the user can say when to delete it - never delete it yourself unprompted.
