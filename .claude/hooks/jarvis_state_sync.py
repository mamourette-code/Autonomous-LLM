#!/usr/bin/env python3
"""Cross-container persistence for JARVIS state, via a git branch.

Not part of the jarvis package - this is Claude Code hook plumbing, invoked
by jarvis-hook.sh. It never imports anything from `jarvis` except read-only,
pre-existing functions (snapshot.inspect, snapshot.latest_valid, store.open_store
to create a fresh empty schema) - nothing here writes to or modifies the
jarvis/ package.

Design (see the conversation this was built from for the full rationale):

- The live `.jarvis/jarvis.db` is WAL-mode SQLite. It is never copied directly;
  every copy goes through sqlite3's online backup API and is then checkpointed
  to a single self-contained file (PRAGMA journal_mode = DELETE), exactly the
  technique jarvis/snapshot.py already uses for its own snapshots.
- The state branch (default `jarvis-state`, override with JARVIS_STATE_BRANCH)
  is checked out as a git worktree OUTSIDE the repo, at
  ~/.jarvis-state-worktrees/<branch>, so testing with a different branch name
  never touches the real one's worktree either.
- The branch holds exactly three things: jarvis.db (a throwaway backup-API
  copy, refreshed on every push), snapshots/snapshot.db(+.json) (the latest
  REAL `jarvis snapshot create` output, refreshed only at PreCompact), and
  jarvis-hook.log.
- Push gate: compare the new backup's sha256 against db.sha256 already
  committed in the worktree. Unchanged DB -> no commit, no push, no exception
  for the log (its fresher content just waits in the worktree's uncommitted
  working copy until the next real push).
- Conflict handling: before pushing, fetch the branch. If its remote head has
  moved past the commit this run started from, do not push to jarvis-state at
  all. Instead, build a new commit (`git commit-tree`) whose tree is this
  run's content and whose parent is the *conflict branch's* current tip (a
  single shared `<branch>-conflict` branch, never force-pushed - append-only),
  and push that. Then adopt the new jarvis-state remote head locally so this
  container's next sync compares against current truth instead of repeating
  the same conflict forever.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


class NoStateBranch(RuntimeError):
    """The state branch doesn't exist yet and this caller may not create it.

    Only cmd_restore (SessionStart) may bootstrap the branch. cmd_sync (Stop,
    PreCompact, SessionEnd) must never seed it - a container's local .jarvis/
    can hold test residue or partial state, and it must never become the seed
    of record for the shared branch.
    """


def _run(cmd: list[str], cwd: str | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=cwd, check=check, capture_output=True, text=True,
    )


def db_logical_hash(path: str) -> str:
    """Hash of a SQLite file's logical content (schema + rows via iterdump),
    not its raw bytes.

    Proven necessary, not theoretical: two backup-API copies taken seconds
    apart from an unchanged live WAL database differed in 3 raw bytes (WAL
    checkpoint/salt housekeeping) while their iterdump() output was
    byte-for-byte identical. A raw-byte hash would have pushed on every Stop.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        h = hashlib.sha256()
        for line in conn.iterdump():
            h.update(line.encode())
        return h.hexdigest()
    finally:
        conn.close()


def backup_db(src: str, dest: str) -> str:
    """Consistent copy via SQLite's online backup API, checkpointed to one file."""
    src_conn = sqlite3.connect(src)
    try:
        dest_conn = sqlite3.connect(dest)
        try:
            src_conn.backup(dest_conn)
            dest_conn.execute("PRAGMA journal_mode = DELETE")
        finally:
            dest_conn.close()
    finally:
        src_conn.close()
    return db_logical_hash(dest)


def make_empty_db(dest: str, project_dir: str) -> None:
    """A fresh schema, zero rows - never seeds a state branch from local test data."""
    sys.path.insert(0, project_dir)
    from jarvis.store import open_store  # noqa: PLC0415

    with open_store(dest):
        pass


def worktree_dir(branch: str) -> str:
    home = os.environ.get("HOME", "/root")
    return str(Path(home) / ".jarvis-state-worktrees" / branch)


def remote_url(project_dir: str) -> str:
    return _run(["git", "remote", "get-url", "origin"], cwd=project_dir).stdout.strip()


def remote_branch_fetch_ok(repo_dir: str, branch: str) -> bool:
    """The authoritative existence check: an actual fetch, not a cached ref.

    `git fetch` never removes a stale local refs/remotes/origin/<branch> on
    its own (no --prune), so a worktree left over from before a remote
    deletion would otherwise look fine forever to anything that only checks
    local state. Fetching fresh, from wherever we're about to read from
    (inside the worktree if it exists, the main repo otherwise), is what
    actually tells us whether the remote branch is still there right now.
    """
    r = _run(["git", "fetch", "-q", "origin", branch], cwd=repo_dir, check=False)
    return r.returncode == 0


def bootstrap_branch(project_dir: str, branch: str) -> None:
    """Create <branch> on origin from an empty schema, in a scratch dir with no
    connection to this repo's history - avoids the leftover-working-tree-files
    trap that `git checkout --orphan` has inside an existing checkout."""
    scratch = Path(f"/tmp/jarvis-state-bootstrap-{os.getpid()}-{int(time.time())}")
    scratch.mkdir(parents=True)
    try:
        _run(["git", "init", "-q"], cwd=str(scratch))
        _run(["git", "checkout", "-q", "-b", branch], cwd=str(scratch))
        db_path = scratch / "jarvis.db"
        make_empty_db(str(db_path), project_dir)
        (scratch / "db.sha256").write_text(db_logical_hash(str(db_path)))
        _run(["git", "add", "jarvis.db", "db.sha256"], cwd=str(scratch))
        _run(["git", "-c", "user.email=jarvis-state@local", "-c", "user.name=jarvis-state-sync",
              "commit", "-q", "-m", "initialize jarvis-state branch: empty schema, no seeded data"],
             cwd=str(scratch))
        _run(["git", "remote", "add", "origin", remote_url(project_dir)], cwd=str(scratch))
        _run(["git", "push", "-q", "-u", "origin", branch], cwd=str(scratch))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _remove_stale_worktree(project_dir: str, wtd: str) -> None:
    _run(["git", "worktree", "remove", "--force", wtd], cwd=project_dir, check=False)
    shutil.rmtree(wtd, ignore_errors=True)
    _run(["git", "worktree", "prune"], cwd=project_dir, check=False)


def ensure_worktree(project_dir: str, branch: str, allow_bootstrap: bool = False) -> str:
    wtd = worktree_dir(branch)
    had_worktree = os.path.isdir(wtd)

    # The remote is checked fresh via fetch every time, regardless of whether
    # a local worktree already exists - a worktree left over from before the
    # branch was deleted on GitHub must never let a later sync resurrect it.
    # Deleting the branch remotely must be a safe reset.
    remote_ok = remote_branch_fetch_ok(wtd if had_worktree else project_dir, branch)

    if not remote_ok:
        if had_worktree:
            _remove_stale_worktree(project_dir, wtd)
        else:
            _run(["git", "worktree", "prune"], cwd=project_dir, check=False)

        if not allow_bootstrap:
            if had_worktree:
                raise NoStateBranch(
                    f"remote branch missing - not recreating (branch={branch!r}); "
                    "stale local worktree removed"
                )
            raise NoStateBranch(
                f"branch {branch!r} does not exist on the remote; only SessionStart's "
                "restore may create it"
            )

        bootstrap_branch(project_dir, branch)
        if not remote_branch_fetch_ok(project_dir, branch):
            raise RuntimeError(
                f"bootstrap of {branch!r} appeared to succeed but the branch is not "
                "fetchable from origin"
            )

    if os.path.isdir(wtd):
        return wtd  # already fetched fresh above, from inside it

    Path(wtd).parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "prune"], cwd=project_dir, check=False)
    have_local_branch = _run(["git", "rev-parse", "--verify", "-q", branch],
                              cwd=project_dir, check=False).returncode == 0
    if have_local_branch:
        r = _run(["git", "worktree", "add", "-q", wtd, branch], cwd=project_dir, check=False)
    else:
        r = _run(["git", "worktree", "add", "-q", "-B", branch, wtd, f"origin/{branch}"],
                  cwd=project_dir, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"git worktree add failed: {r.stderr.strip()}")
    return wtd


def patch_snapshot_sidecar_path(json_path: str, new_db_path: str) -> None:
    with open(json_path) as fh:
        data = json.load(fh)
    data["path"] = new_db_path
    with open(json_path, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)


def cmd_restore(project_dir: str, branch: str) -> int:
    # The only caller allowed to bootstrap the branch from an empty schema.
    wtd = ensure_worktree(project_dir, branch, allow_bootstrap=True)
    _run(["git", "reset", "-q", "--hard", f"origin/{branch}"], cwd=wtd)

    jarvis_dir = Path(project_dir) / ".jarvis"
    jarvis_dir.mkdir(exist_ok=True)
    log_path = Path(project_dir) / ".claude" / "hooks" / "jarvis-hook.log"
    wtd_log = Path(wtd) / "jarvis-hook.log"
    if wtd_log.exists() and not (log_path.exists() and log_path.stat().st_size > 0):
        log_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(wtd_log, log_path)

    db_dest = jarvis_dir / "jarvis.db"
    wtd_db = Path(wtd) / "jarvis.db"
    if not wtd_db.exists():
        print("RESTORE_FAILED no jarvis.db found on state branch")
        return 1
    shutil.copyfile(wtd_db, db_dest)

    wtd_snap_db = Path(wtd) / "snapshots" / "snapshot.db"
    wtd_snap_json = Path(wtd) / "snapshots" / "snapshot.db.json"
    if wtd_snap_db.exists() and wtd_snap_json.exists():
        snap_dir = jarvis_dir / "snapshots"
        snap_dir.mkdir(exist_ok=True)
        local_db = snap_dir / "snapshot.db"
        local_json = snap_dir / "snapshot.db.json"
        shutil.copyfile(wtd_snap_db, local_db)
        shutil.copyfile(wtd_snap_json, local_json)
        patch_snapshot_sidecar_path(str(local_json), str(local_db))

    sys.path.insert(0, project_dir)
    from jarvis import snapshot as snapshot_mod  # noqa: PLC0415

    check = snapshot_mod.inspect(str(db_dest))
    remote_head = _run(["git", "rev-parse", "origin/" + branch], cwd=wtd).stdout.strip()
    if not check["ok"]:
        corrupt_path = jarvis_dir / f"jarvis.db.corrupt-{int(time.time())}"
        shutil.move(str(db_dest), str(corrupt_path))
        print(f"RESTORE_FAILED integrity={check['integrity']} fk={check['foreign_key_check']} "
              f"remote_head={remote_head[:12]} moved-to={corrupt_path.name}")
        return 1

    print(f"RESTORE_OK remote_head={remote_head[:12]} tables={check['tables']}")
    return 0


def find_local_latest_snapshot(project_dir: str):
    sys.path.insert(0, project_dir)
    from jarvis import snapshot as snapshot_mod  # noqa: PLC0415

    snap_dir = Path(project_dir) / ".jarvis" / "snapshots"
    return snapshot_mod.latest_valid(directory=str(snap_dir))


def cmd_sync(project_dir: str, branch: str, event: str, claude_session: str, update_snapshot: bool) -> int:
    try:
        # allow_bootstrap defaults to False: a Stop/PreCompact/SessionEnd sync
        # must never create the state branch. If it doesn't exist yet, there
        # is nothing to push to and nothing to do - only SessionStart's
        # restore may seed it, and only from an empty schema, never from this
        # container's local .jarvis/ (which may hold test residue).
        wtd = ensure_worktree(project_dir, branch)
    except NoStateBranch as exc:
        print(f"NOOP {exc}")
        return 0

    base_sha = _run(["git", "rev-parse", "HEAD"], cwd=wtd).stdout.strip()

    sha_file = Path(wtd) / "db.sha256"
    old_hash = sha_file.read_text().strip() if sha_file.exists() else ""

    live_db = Path(project_dir) / ".jarvis" / "jarvis.db"
    if not live_db.exists():
        print("NOOP no local .jarvis/jarvis.db to sync")
        return 0

    try:
        new_hash = backup_db(str(live_db), str(Path(wtd) / "jarvis.db"))

        snapshot_changed = False
        if update_snapshot:
            latest = find_local_latest_snapshot(project_dir)
            if latest is not None:
                snap_dir = Path(wtd) / "snapshots"
                snap_dir.mkdir(exist_ok=True)
                dest_db = snap_dir / "snapshot.db"
                prior_hash = db_logical_hash(str(dest_db)) if dest_db.exists() else None
                new_snap_hash = db_logical_hash(latest.path)
                if new_snap_hash != prior_hash:
                    shutil.copyfile(latest.path, dest_db)
                    shutil.copyfile(latest.sidecar, snap_dir / "snapshot.db.json")
                    snapshot_changed = True
    except (sqlite3.Error, OSError) as exc:
        # A failed backup must never be pushed. The worktree's jarvis.db may
        # now hold a partial write from the failed backup attempt, but
        # nothing has been added, committed, or pushed - `git status` in the
        # worktree still shows only that unstaged, harmless difference.
        print(f"BACKUP_FAILED {type(exc).__name__}: {exc} - push skipped")
        return 0

    log_src = Path(project_dir) / ".claude" / "hooks" / "jarvis-hook.log"
    if log_src.exists():
        shutil.copyfile(log_src, Path(wtd) / "jarvis-hook.log")

    if new_hash == old_hash and not snapshot_changed:
        print(f"NOOP hash={new_hash[:12]} unchanged since last push")
        return 0

    sha_file.write_text(new_hash)
    _run(["git", "add", "-A"], cwd=wtd)
    commit = _run(
        ["git", "-c", "user.email=jarvis-state@local", "-c", "user.name=jarvis-state-sync",
         "commit", "-q", "-m",
         f"state sync: claude_session={claude_session} event={event} hash={new_hash[:12]}"],
        cwd=wtd, check=False,
    )
    if commit.returncode != 0:
        # `git add -A` found nothing to commit (can happen if only file mtimes
        # changed) - treat like any other no-op.
        print(f"NOOP hash={new_hash[:12]} nothing to commit")
        return 0
    new_commit_sha = _run(["git", "rev-parse", "HEAD"], cwd=wtd).stdout.strip()

    _run(["git", "fetch", "-q", "origin", branch], cwd=wtd)
    remote_sha = _run(["git", "rev-parse", f"origin/{branch}"], cwd=wtd).stdout.strip()

    if remote_sha == base_sha:
        push = _run(["git", "push", "-q", "origin", f"HEAD:{branch}"], cwd=wtd, check=False)
        if push.returncode != 0:
            print(f"PUSH_FAILED {push.stderr.strip()[:300]}")
            return 0
        print(f"PUSHED hash={new_hash[:12]} commit={new_commit_sha[:12]}")
        return 0

    # CONFLICT: remote moved since we started. Never push to <branch>.
    conflict_branch = f"{branch}-conflict"
    _run(["git", "fetch", "-q", "origin", conflict_branch], cwd=wtd, check=False)
    # --verify -q: a plain `git rev-parse <bad-ref>` echoes the unresolved
    # string back to stdout instead of failing empty, which would otherwise
    # smuggle a bogus ref into `commit-tree -p` below when the conflict
    # branch doesn't exist yet.
    conflict_tip_probe = _run(["git", "rev-parse", "--verify", "-q", f"origin/{conflict_branch}"],
                               cwd=wtd, check=False)
    conflict_tip = conflict_tip_probe.stdout.strip() if conflict_tip_probe.returncode == 0 else ""
    tree = _run(["git", "rev-parse", "HEAD^{tree}"], cwd=wtd).stdout.strip()
    msg = (f"CONFLICT: claude_session={claude_session} event={event} "
           f"base={base_sha[:12]} remote_found={remote_sha[:12]}")
    if conflict_tip:
        new_commit = _run(["git", "commit-tree", tree, "-p", conflict_tip, "-m", msg], cwd=wtd).stdout.strip()
    else:
        new_commit = _run(["git", "commit-tree", tree, "-m", msg], cwd=wtd).stdout.strip()
    push = _run(["git", "push", "-q", "origin", f"{new_commit}:refs/heads/{conflict_branch}"], cwd=wtd, check=False)
    if push.returncode != 0:
        print(f"CONFLICT_PUSH_FAILED base={base_sha[:12]} remote_found={remote_sha[:12]} "
              f"error={push.stderr.strip()[:200]}")
        return 0
    # Adopt the new remote truth locally so the next sync compares against it,
    # instead of reporting the same conflict on every future push.
    _run(["git", "reset", "-q", "--hard", f"origin/{branch}"], cwd=wtd)
    print(f"CONFLICT base={base_sha[:12]} remote_found={remote_sha[:12]} "
          f"conflict_branch={conflict_branch} conflict_commit={new_commit[:12]}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: jarvis_state_sync.py restore|sync ...", file=sys.stderr)
        return 2
    action = argv[1]
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    branch = os.environ.get("JARVIS_STATE_BRANCH", "jarvis-state")

    if action == "restore":
        return cmd_restore(project_dir, branch)
    if action == "sync":
        if len(argv) < 4:
            print("usage: jarvis_state_sync.py sync <event> <claude_session_id> [--update-snapshot]",
                  file=sys.stderr)
            return 2
        event, claude_session = argv[2], argv[3]
        update_snapshot = "--update-snapshot" in argv[4:]
        return cmd_sync(project_dir, branch, event, claude_session, update_snapshot)

    print(f"unknown action {action!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
