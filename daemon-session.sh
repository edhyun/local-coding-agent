#!/usr/bin/env bash
# Start or attach to the 24/7 council-queue daemon's tmux session for a repo.
#
# Unlike agent-session.sh/chief-session.sh (one-shot REPLs you type goals
# into), this runs chief.py --daemon: it drains .agent-queue continuously
# through the full Engineer -> QA -> PM pipeline, never exits on an empty
# queue (polls and waits for more work), and only stops on an explicit
# signal - touch .agent-queue/STOP in the repo, or Ctrl-C this session.
# Add tasks to its queue from the dashboard's queue panel while this keeps
# running; attaching here only shows its log (Ctrl-b d to detach, same as
# the others) - reviewing anything it flags "needs_human" happens by
# looking at the dashboard or agent-run-log.jsonl, same as any other run.
#
# It never auto-pushes and it never runs two things at once against the
# same repo by itself - but it IS a second process that can check out
# branches and commit, same caveat as running agent-session.sh and
# chief-session.sh together: don't also run those, or the dashboard's
# submit form, against this repo while the daemon is active. The dashboard
# refuses submit/push while its heartbeat looks alive, but nothing stops
# another terminal REPL from colliding with it - that's still on you.
set -euo pipefail

if [ $# -lt 1 ] || [ $# -gt 2 ]; then
  echo "usage: daemon-session.sh <repo-path> [model]" >&2
  exit 2
fi

REPO="$(cd "$1" && pwd)"
NAME="daemon-$(basename "$REPO")"
CHIEF="$(cd "$(dirname "$0")" && pwd)/chief.py"

if tmux has-session -t "$NAME" 2>/dev/null; then
  echo "Session $NAME is already running - attaching (Ctrl-b d to detach)."
  exec tmux attach -t "$NAME"
else
  echo "Starting 24/7 daemon for $REPO"
  if [ $# -eq 2 ]; then
    exec tmux new-session -s "$NAME" -c "$REPO" \
      "python3 '$CHIEF' --daemon '$REPO' --model '$2'"
  else
    exec tmux new-session -s "$NAME" -c "$REPO" \
      "python3 '$CHIEF' --daemon '$REPO'"
  fi
fi
