#!/usr/bin/env python3
"""
Read-only local web dashboard for orchestrator.py / chief.py (2026-08-26).

Deliberately the simplest thing that works: stdlib only (http.server), no
new dependency, no build step, one HTML page with a small inline script
that polls two JSON endpoints. It does NOT submit goals or approve
pushes - that stays in the tmux REPL (agent-session.sh / chief-session.sh),
same human-approval-gate boundary as everywhere else in this project. This
is a second, independent way to watch the SAME repo-local state those
already produce, not a new control surface.

How it sees anything: orchestrator.py's invoke_opencode() (used by both
run_task and chief.py's run_review) already writes every streamed model
event to `<repo>/.agent-dashboard/events.jsonl`, tagged with a run_id,
role (Engineer/QA/PM), and round. This process just tails that file - no
in-process pub/sub, no shared state with whatever orchestrator/chief
process is actually running. History comes from the existing
`agent-run-log.jsonl` the wrapper already wrote for its own record-keeping.

Binds to 127.0.0.1 only by default. There is no authentication - anyone
who can reach the bound host can read your task history and code diffs.
Pass --host 0.0.0.0 explicitly if you want it reachable from another
device on your network; that is an opt-in, not the default, precisely
because there's no auth in front of it.
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from orchestrator import DASHBOARD_DIR, DASHBOARD_EVENTS_FILE

HISTORY_FILE = "agent-run-log.jsonl"
MAX_HISTORY = 100

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Chief of Staff Dashboard</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, "SF Mono", Menlo, monospace;
    background: #14151a; color: #d8dade; font-size: 13px;
  }
  header {
    padding: 12px 18px; border-bottom: 1px solid #2a2c34;
    display: flex; justify-content: space-between; align-items: center;
  }
  header h1 { font-size: 15px; margin: 0; font-weight: 600; }
  header .repo { color: #7c7f8a; font-size: 12px; }
  #layout { display: flex; height: calc(100vh - 49px); }
  #feed, #history { overflow-y: auto; padding: 14px 18px; }
  #feed { flex: 2; border-right: 1px solid #2a2c34; }
  #history { flex: 1; min-width: 280px; }
  h2 { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
       color: #7c7f8a; margin: 0 0 10px; }
  .run { margin-bottom: 18px; border: 1px solid #23252c; border-radius: 6px; overflow: hidden; }
  .run-head { padding: 6px 10px; background: #1b1c22; font-size: 11px; color: #9a9dab;
              display: flex; justify-content: space-between; }
  .role-block { padding: 8px 10px; border-top: 1px solid #23252c; }
  .role-label { display: inline-block; font-weight: 700; font-size: 11px;
                padding: 1px 6px; border-radius: 3px; margin-bottom: 6px; }
  .role-Engineer { background: #1c3352; color: #7fb2ff; }
  .role-QA { background: #3a2e14; color: #e5b567; }
  .role-PM { background: #163a24; color: #7bd99a; }
  .role-active .role-label { box-shadow: 0 0 0 1px currentColor; }
  .line { padding: 1px 0; color: #b7bac4; white-space: pre-wrap; word-break: break-word; }
  .line.tool { color: #8b8ea0; }
  .line.err { color: #ff8686; }
  .end-status { margin-top: 4px; font-size: 11px; color: #7c7f8a; }
  .hist-item { padding: 8px 0; border-bottom: 1px solid #23252c; }
  .hist-status { font-weight: 700; font-size: 11px; padding: 1px 6px; border-radius: 3px; }
  .st-APPROVED, .st-COMMITTED, .st-PR_OPENED { background: #163a24; color: #7bd99a; }
  .st-FAILED, .st-BLOCKED_GATE_FAIL, .st-PR_FAILED, .st-NEEDS_HUMAN { background: #3a1616; color: #ff8686; }
  .st-ESCALATED, .st-REFUSED_DIRTY { background: #3a2e14; color: #e5b567; }
  .hist-task { color: #d8dade; margin-top: 3px; }
  .hist-time { color: #6a6d78; font-size: 11px; margin-top: 3px; }
  #empty { color: #6a6d78; padding: 20px 0; }
</style>
</head>
<body>
<header>
  <h1>Chief of Staff - live monitor</h1>
  <span class="repo" id="repo-path"></span>
</header>
<div id="layout">
  <div id="feed"><h2>Live feed (read-only)</h2><div id="runs"></div></div>
  <div id="history"><h2>History</h2><div id="hist-list"><div id="empty">No runs yet.</div></div></div>
</div>
<script>
let offset = 0;
const runs = {}; // run_id -> { el, roles: { role -> {el, started, ended} } }

function roleBlock(runEl, run_id, role) {
  const key = run_id + ':' + role;
  if (runs[run_id].roles[key]) return runs[run_id].roles[key];
  const el = document.createElement('div');
  el.className = 'role-block role-' + role;
  el.innerHTML = '<span class="role-label role-' + role + '">' + role + '</span><div class="lines"></div>';
  runEl.appendChild(el);
  const rec = { el, lines: el.querySelector('.lines'), ended: false };
  runs[run_id].roles[key] = rec;
  return rec;
}

function runBlock(run_id) {
  if (runs[run_id]) return runs[run_id];
  const el = document.createElement('div');
  el.className = 'run';
  el.innerHTML = '<div class="run-head"><span>' + run_id + '</span><span class="round"></span></div>';
  document.getElementById('runs').prepend(el);
  runs[run_id] = { el, roles: {} };
  return runs[run_id];
}

function addLine(rec, text, cls) {
  const d = document.createElement('div');
  d.className = 'line' + (cls ? ' ' + cls : '');
  d.textContent = text;
  rec.lines.appendChild(d);
}

function applyEvent(e) {
  const run = runBlock(e.run_id);
  const rec = roleBlock(run.el, e.run_id, e.role);
  run.el.querySelector('.round').textContent = 'round ' + (e.round || 1);
  if (e.kind === 'start') {
    rec.el.classList.add('role-active');
    addLine(rec, 'task: ' + e.task, 'tool');
  } else if (e.kind === 'opencode_event') {
    const ev = e.event || {};
    const part = ev.part || {};
    if (ev.type === 'text') {
      const t = (part.text || '').trim();
      if (t) addLine(rec, t.slice(0, 300));
    } else if (ev.type === 'tool_use') {
      const state = part.state || {};
      const tool = part.tool || '?';
      const status = state.status || '?';
      let extra = '';
      if (tool === 'write') extra = (state.input || {}).filePath || '';
      if (tool === 'bash') extra = (state.input || {}).command || '';
      addLine(rec, '→ ' + tool + (extra ? ' ' + extra.slice(0, 80) : '') + ' [' + status + ']',
              status === 'error' ? 'err' : 'tool');
    }
  } else if (e.kind === 'end') {
    rec.el.classList.remove('role-active');
    rec.ended = true;
    const d = document.createElement('div');
    d.className = 'end-status';
    d.textContent = e.role + ' finished: ' + e.status;
    rec.el.appendChild(d);
  }
}

async function pollEvents() {
  try {
    const res = await fetch('/api/events?offset=' + offset);
    const data = await res.json();
    for (const line of data.lines) applyEvent(line);
    offset = data.offset;
  } catch (e) { /* dashboard server may be mid-restart; just retry next tick */ }
  setTimeout(pollEvents, 1000);
}

function statusClass(status) { return 'st-' + (status || 'UNKNOWN'); }

async function pollHistory() {
  try {
    const res = await fetch('/api/history');
    const data = await res.json();
    const list = document.getElementById('hist-list');
    if (data.entries.length === 0) {
      list.innerHTML = '<div id="empty">No runs yet.</div>';
    } else {
      list.innerHTML = data.entries.map(e => {
        const status = (e.chief_report && e.chief_report.status) || e.status || 'UNKNOWN';
        const task = (e.chief_report && e.chief_report.engineer && e.chief_report.engineer.branch) || e.task || e.branch || '';
        return '<div class="hist-item"><span class="hist-status ' + statusClass(status) + '">' + status + '</span>' +
               '<div class="hist-task">' + task + '</div>' +
               '<div class="hist-time">' + (e.timestamp || '') + '</div></div>';
      }).join('');
    }
  } catch (e) { /* retry next tick */ }
  setTimeout(pollHistory, 3000);
}

document.getElementById('repo-path').textContent = window.location.search.slice(1) ? '' : '';
pollEvents();
pollHistory();
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    repo: Path = None  # set by main() before serve_forever

    def log_message(self, fmt, *args):
        pass  # keep the terminal quiet - this is meant to be watched in a browser

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/events":
            qs = parse_qs(parsed.query)
            offset = int(qs.get("offset", ["0"])[0])
            events_path = self.repo / DASHBOARD_DIR / DASHBOARD_EVENTS_FILE
            lines = []
            new_offset = offset
            if events_path.exists():
                with events_path.open("rb") as f:
                    f.seek(offset)
                    chunk = f.read()
                    new_offset = offset + len(chunk)
                for raw in chunk.decode(errors="replace").splitlines():
                    if not raw.strip():
                        continue
                    try:
                        lines.append(json.loads(raw))
                    except json.JSONDecodeError:
                        continue
            self._json({"lines": lines, "offset": new_offset})
            return

        if parsed.path == "/api/history":
            log_path = self.repo / HISTORY_FILE
            entries = []
            if log_path.exists():
                for raw in log_path.read_text().splitlines():
                    if not raw.strip():
                        continue
                    try:
                        entries.append(json.loads(raw))
                    except json.JSONDecodeError:
                        continue
            entries = list(reversed(entries))[:MAX_HISTORY]
            self._json({"entries": entries})
            return

        self._json({"error": "not found"}, status=404)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1",
                         help="Bind address. Default is localhost-only - "
                              "there is no auth, so opening this to 0.0.0.0 "
                              "is a deliberate opt-in, not the default.")
    args = parser.parse_args()

    Handler.repo = args.repo.resolve()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Dashboard for {Handler.repo} - http://{args.host}:{args.port}")
    print("Read-only: submit goals and approve pushes via the tmux REPL, not here.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
