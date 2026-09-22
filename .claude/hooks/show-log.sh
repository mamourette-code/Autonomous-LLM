#!/usr/bin/env bash
# Merges every per-session log under .claude/hooks/logs/*.log into one
# timestamp-sorted stream, so a container that only has its own session's
# log (plus whatever other sessions' logs restore copied down) can still see
# a combined view. Each entry is tagged with its source session id. A
# multi-line entry (e.g. a Python traceback logged as one message) stays
# attached to the timestamped line that started it - lines are grouped by
# their leading "YYYY-MM-DDTHH:MM:SSZ " timestamp before sorting, not sorted
# line-by-line.
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOGS_DIR="$SCRIPT_DIR/logs"

shopt -s nullglob
files=("$LOGS_DIR"/*.log)
if [ ${#files[@]} -eq 0 ]; then
  echo "no session logs found under $LOGS_DIR" >&2
  exit 0
fi

python3 - "${files[@]}" <<'PY'
import re
import sys

TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)\s")

entries = []
for path in sys.argv[1:]:
    name = path.rsplit("/", 1)[-1]
    session = name[:-4] if name.endswith(".log") else name
    current_ts = None
    current_lines = []

    def flush():
        if current_lines:
            entries.append((current_ts or "", session, list(current_lines)))

    with open(path, "r", errors="replace") as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            m = TS_RE.match(line)
            if m:
                flush()
                current_ts = m.group(1)
                current_lines = [line]
            else:
                current_lines.append(line)
        flush()

entries.sort(key=lambda e: (e[0], e[1]))
for ts, session, lines in entries:
    print(f"[{session}] {lines[0]}")
    for extra in lines[1:]:
        print(extra)
PY
