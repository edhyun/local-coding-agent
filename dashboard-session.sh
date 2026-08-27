#!/usr/bin/env bash
# Start or attach to the web dashboard's tmux session for a repo.
#
# Wrapped in tmux for the same reason as agent-session.sh/chief-session.sh -
# consistent start/stop/attach muscle memory. Attaching shows the server's
# own log lines (intentionally sparse, see dashboard.py's log_message
# override), not the dashboard UI itself - actually submitting goals or
# approving pushes happens in the browser page at the printed URL, not in
# this terminal. Ctrl-C there stops the server, Ctrl-b d leaves it running.
set -euo pipefail

if [ $# -lt 1 ] || [ $# -gt 2 ]; then
  echo "usage: dashboard-session.sh <repo-path> [port]" >&2
  exit 2
fi

REPO="$(cd "$1" && pwd)"
PORT="${2:-8765}"
NAME="dashboard-$(basename "$REPO")"
DASHBOARD="$(cd "$(dirname "$0")" && pwd)/dashboard.py"

if tmux has-session -t "$NAME" 2>/dev/null; then
  echo "Session $NAME is already running - open http://127.0.0.1:$PORT (Ctrl-b d to detach from the log view)."
  exec tmux attach -t "$NAME"
else
  echo "Starting dashboard for $REPO at http://127.0.0.1:$PORT"
  exec tmux new-session -s "$NAME" -c "$REPO" \
    "python3 '$DASHBOARD' '$REPO' --port '$PORT'"
fi
