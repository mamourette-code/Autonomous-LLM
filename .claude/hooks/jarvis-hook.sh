#!/usr/bin/env bash
# Wires JARVIS (python3 -m jarvis) into Claude Code's session lifecycle hooks.
#
# Invocation (from .claude/settings.json):
#   jarvis-hook.sh boot                              # SessionStart
#   jarvis-hook.sh snapshot create --reason pre-compact   # PreCompact
#   jarvis-hook.sh close                             # SessionEnd
#   jarvis-hook.sh stop                              # Stop (state sync only)
#
# Each hook receives the event's JSON payload on stdin (session_id plus
# event-specific fields such as `source` or `reason`). This script maps
# Claude Code's session_id to the JARVIS session_id it booted, one mapping
# file per Claude Code session under .jarvis/hook_sessions/, so concurrent
# Claude Code sessions never share or clobber each other's JARVIS session.
#
# Cross-container persistence: .jarvis/ is gitignored and lost between fresh
# containers, so state is mirrored to a git branch (jarvis_state_sync.py,
# default branch `jarvis-state`, override with JARVIS_STATE_BRANCH). SessionStart
# restores from it before booting; PreCompact/SessionEnd/Stop push to it when
# content changed. See jarvis_state_sync.py's module docstring for the design.
#
# Every outcome (success or failure) is appended to this Claude Code session's
# own log file, .claude/hooks/logs/<claude_session_id>.log - never a single
# shared file, so two containers writing concurrently can never clobber each
# other's lines. Run .claude/hooks/show-log.sh for a merged, timestamp-sorted
# view across every session's log. This script always exits 0: a JARVIS
# failure must never break a Claude Code session. Failures are recorded in
# the log, never left silently unrecorded.
#
# .claude/hooks/jarvis-hook.log (the old single shared log) is no longer
# written to. It is left in place, and its last committed copy stays on the
# state branch as a frozen legacy file - nothing reads or writes it anymore.
#
# Orphan policy: a JARVIS session whose Claude Code session ended without a
# SessionEnd hook firing (crash, killed terminal) is logged as an orphan and
# left open. It is never auto-closed with a fabricated summary - an open
# session with no close event is real evidence, not a bug to paper over.

set -u

# Project root: prefer Claude Code's own placeholder so this never depends on
# the hook's ambient cwd; fall back to resolving relative to this script's own
# location (two levels up from .claude/hooks/) if run outside Claude Code.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${CLAUDE_PROJECT_DIR:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
cd "$PROJECT_DIR" || exit 0

# DB resolution must never depend on cwd: set JARVIS_HOME explicitly in
# addition to cd'ing into the project root.
export JARVIS_HOME="$PROJECT_DIR/.jarvis"

PYTHON="/usr/local/bin/python3"
MAP_DIR="$JARVIS_HOME/hook_sessions"
STATE_SYNC="$SCRIPT_DIR/jarvis_state_sync.py"
LOGS_DIR="$PROJECT_DIR/.claude/hooks/logs"
mkdir -p "$MAP_DIR" "$LOGS_DIR"

STDIN_JSON="$(cat)"
CC_SESSION_ID="$(printf '%s' "$STDIN_JSON" | jq -r '.session_id // "unknown"')"
LOG="$LOGS_DIR/$CC_SESSION_ID.log"
EVENT="${1:-}"
shift || true

log() {
  # $1 = exit code, $2 = optional message (includes stderr on failure)
  local ts
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '%s event=%s claude_session=%s exit=%s %s\n' \
    "$ts" "$EVENT" "$CC_SESSION_ID" "$1" "${2:-}" >> "$LOG"
}

MAP_FILE="$MAP_DIR/$CC_SESSION_ID"

# Returns 0 if a session.close event exists for $1, 1 if none found, 2 if the
# lookup itself failed (DB error) - callers must not treat 2 as "open".
has_session_close_event() {
  local jid="$1" evjson rc
  evjson="$("$PYTHON" -m jarvis events --target "$jid" --json 2>/dev/null)"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    return 2
  fi
  printf '%s' "$evjson" | jq -e 'any(.[]; .kind == "session.close")' >/dev/null 2>&1
}

# Scans every OTHER mapping file for a JARVIS session with no session.close
# event and logs it as an orphan. Never deletes or closes an open one. A
# mapping whose JARVIS session turns out to already be closed (e.g. someone
# closed it by hand) is a stale-but-resolved mapping, not an orphan - that one
# is safe to remove since nothing is lost.
scan_orphans() {
  local f other_cc other_jid rc
  for f in "$MAP_DIR"/*; do
    [ -e "$f" ] || continue
    other_cc="$(basename "$f")"
    [ "$other_cc" = "$CC_SESSION_ID" ] && continue
    other_jid="$(cat "$f" 2>/dev/null)"
    [ -n "$other_jid" ] || continue
    if has_session_close_event "$other_jid"; then
      rm -f "$f"
      log 0 "cleanup: stale mapping for already-closed jarvis session $other_jid (claude_session=$other_cc) removed"
    else
      rc=$?
      if [ "$rc" -eq 2 ]; then
        log 1 "could not verify status of jarvis session $other_jid (claude_session=$other_cc): events lookup failed"
      else
        log 0 "ORPHAN: jarvis session $other_jid (claude_session=$other_cc) has no SessionEnd - still open, left as-is"
      fi
    fi
  done
}

# Pushes the current .jarvis/ state to the state branch if it changed, then
# always exits 0. $1 = event name for the commit message / log line; any
# further args (e.g. --update-snapshot) are passed through to the sync
# subcommand. A no-op DB (nothing changed since the last push) never pushes.
sync_and_exit() {
  local event="$1"
  shift || true
  if [ -f "$JARVIS_HOME/jarvis.db" ]; then
    local out rc
    out="$("$PYTHON" "$STATE_SYNC" sync "$event" "$CC_SESSION_ID" "$@" 2>&1)"
    rc=$?
    log "$rc" "$out"
  else
    log 0 "$event: no local .jarvis/jarvis.db, nothing to sync"
  fi
  exit 0
}

case "$EVENT" in
  boot)
    SOURCE="$(printf '%s' "$STDIN_JSON" | jq -r '.source // "unknown"')"

    if [ ! -f "$JARVIS_HOME/jarvis.db" ]; then
      # First SessionStart in this container's lifetime (or a corrupt restore
      # was moved aside earlier) - restore from the state branch before doing
      # anything else. Never boot on top of a restore that fails integrity.
      RESTORE_OUT="$("$PYTHON" "$STATE_SYNC" restore "$CC_SESSION_ID" 2>&1)"
      RESTORE_RC=$?
      log "$RESTORE_RC" "$RESTORE_OUT"
      if [ "$RESTORE_RC" -ne 0 ]; then
        echo "JARVIS RESTORE FAILED - session not tracked, see $LOG"
        exit 0
      fi
    fi

    scan_orphans

    if [ -f "$MAP_FILE" ]; then
      # SessionStart also fires on resume/clear/compact/fork, not just
      # startup. A mapping already exists for this Claude Code session, so
      # the JARVIS session is still active - re-print a short status instead
      # of booting a second JARVIS session on top of it.
      JARVIS_ID="$(cat "$MAP_FILE")"
      DOCTOR_JSON="$("$PYTHON" -m jarvis doctor --json 2>/tmp/jarvis_hook_err.$$)"
      RC=$?
      if [ "$RC" -ne 0 ]; then
        ERR="$(cat /tmp/jarvis_hook_err.$$ 2>/dev/null)"
        rm -f /tmp/jarvis_hook_err.$$
        log "$RC" "source=$SOURCE doctor(existing session $JARVIS_ID) failed: $ERR"
        exit 0
      fi
      rm -f /tmp/jarvis_hook_err.$$
      HEALTH="$(printf '%s' "$DOCTOR_JSON" | jq -r '.health.status')"
      OPEN_TASKS="$(printf '%s' "$DOCTOR_JSON" | jq '.open_tasks | length')"
      echo "jarvis session $JARVIS_ID already active (source=$SOURCE) - health: $HEALTH, open tasks: $OPEN_TASKS"
      log 0 "source=$SOURCE reused existing jarvis session $JARVIS_ID"
      exit 0
    fi

    BOOT_JSON="$("$PYTHON" -m jarvis boot --json 2>/tmp/jarvis_hook_err.$$)"
    RC=$?
    if [ "$RC" -ne 0 ]; then
      ERR="$(cat /tmp/jarvis_hook_err.$$ 2>/dev/null)"
      rm -f /tmp/jarvis_hook_err.$$
      log "$RC" "source=$SOURCE boot failed: $ERR"
      echo "[jarvis-hook] boot failed - see $LOG"
      exit 0
    fi
    rm -f /tmp/jarvis_hook_err.$$

    JARVIS_ID="$(printf '%s' "$BOOT_JSON" | jq -r '.session_id')"
    echo "$JARVIS_ID" > "$MAP_FILE"

    HEALTH="$(printf '%s' "$BOOT_JSON" | jq -r '.health.status')"
    OPEN_OBJ="$(printf '%s' "$BOOT_JSON" | jq '.open_objectives | length')"
    READY="$(printf '%s' "$BOOT_JSON" | jq '.ready_tasks | length')"
    BLOCKED="$(printf '%s' "$BOOT_JSON" | jq '.blocked_tasks | length')"
    FAILURES="$(printf '%s' "$BOOT_JSON" | jq '.recent_failures | length')"
    UNVERIFIED="$(printf '%s' "$BOOT_JSON" | jq '.unverified_completions | length')"
    echo "jarvis session $JARVIS_ID started (source=$SOURCE) - health: $HEALTH | objectives:$OPEN_OBJ ready:$READY blocked:$BLOCKED failures:$FAILURES unverified:$UNVERIFIED"
    log 0 "source=$SOURCE booted jarvis session $JARVIS_ID"
    ;;

  snapshot)
    OUT="$("$PYTHON" -m jarvis snapshot "$@" 2>&1)"
    RC=$?
    if [ "$RC" -ne 0 ]; then
      log "$RC" "snapshot $* failed: $OUT"
      # No fresh named snapshot to publish, but still flush the DB backup.
      sync_and_exit precompact
    else
      log 0 "snapshot $* ok"
      sync_and_exit precompact --update-snapshot
    fi
    ;;

  close)
    REASON="$(printf '%s' "$STDIN_JSON" | jq -r '.reason // "unknown"')"

    if [ ! -f "$MAP_FILE" ]; then
      log 0 "no jarvis session mapped for this claude session (reason=$REASON) - nothing to close"
      sync_and_exit close
    fi

    JARVIS_ID="$(cat "$MAP_FILE")"
    SUMMARY="session ended (auto-closed by SessionEnd hook, reason: $REASON)"
    OUT="$("$PYTHON" -m jarvis close "$JARVIS_ID" --summary "$SUMMARY" 2>&1)"
    RC=$?
    if [ "$RC" -ne 0 ]; then
      if has_session_close_event "$JARVIS_ID"; then
        # Already closed by something else (e.g. a human ran `jarvis close`
        # by hand) - the mapping is stale-but-resolved, safe to remove.
        rm -f "$MAP_FILE"
        log 0 "reason=$REASON jarvis session $JARVIS_ID was already closed elsewhere; mapping removed"
      else
        # Genuine failure to close - leave the mapping in place. This is
        # exactly the orphan case: a future boot's scan will find and log it.
        log "$RC" "reason=$REASON close $JARVIS_ID failed: $OUT"
      fi
      sync_and_exit close
    fi

    rm -f "$MAP_FILE"
    log 0 "reason=$REASON closed jarvis session $JARVIS_ID"
    sync_and_exit close
    ;;

  stop)
    sync_and_exit stop
    ;;

  *)
    log 1 "unrecognized or missing event '$EVENT'"
    ;;
esac

exit 0
