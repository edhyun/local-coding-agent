#!/usr/bin/env bash
# Start or attach to the Chief of Staff's tmux session for a repo.
#
# Same D1 interactive/detachable model as agent-session.sh: attaching
# watches the council run live (Engineer, then QA, then PM, streamed);
# detaching (Ctrl-b d) leaves it running. Session name is "chief-<repo>",
# distinct from "agent-<repo>" (orchestrator.py's own session), so you can
# run the single-specialist executor and the Chief of Staff layer against
# the same repo without them colliding - though running both against the
# same repo at once is on you to avoid, same as any two people pushing to
# one working tree.
set -euo pipefail

if [ $# -ne 1 ]; then
  echo "usage: chief-session.sh <repo-path>" >&2
  exit 2
fi

REPO="$(cd "$1" && pwd)"
NAME="chief-$(basename "$REPO")"
CHIEF="$(cd "$(dirname "$0")" && pwd)/chief.py"

if tmux has-session -t "$NAME" 2>/dev/null; then
  echo "Session $NAME is already running - attaching (Ctrl-b d to detach)."
  exec tmux attach -t "$NAME"
else
  echo "Starting session $NAME for $REPO"
  exec tmux new-session -s "$NAME" -c "$REPO" \
    "python3 '$CHIEF' --repl '$REPO'"
fi
