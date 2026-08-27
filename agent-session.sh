#!/usr/bin/env bash
# Start or attach to the agent's tmux session for a repo (D1, D3-addendum).
#
# Session naming: agent-<repo-basename> - v1 assumes one agent per repo at
# a time, so this always finds the right session. Attaching means watching
# the REPL live; detaching (Ctrl-b d) leaves the same process running,
# working through anything queued, until you reattach.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: agent-session.sh <repo-path>" >&2
  exit 2
fi

REPO="$(cd "$1" && pwd)"
NAME="agent-$(basename "$REPO")"
ORCHESTRATOR="$(cd "$(dirname "$0")" && pwd)/orchestrator.py"

if tmux has-session -t "$NAME" 2>/dev/null; then
  echo "Session $NAME is already running - attaching (Ctrl-b d to detach)."
  exec tmux attach -t "$NAME"
else
  echo "Starting session $NAME for $REPO"
  exec tmux new-session -s "$NAME" -c "$REPO" \
    "python3 '$ORCHESTRATOR' --repl '$REPO'"
fi
