#!/usr/bin/env python3
"""
Local web dashboard for orchestrator.py / chief.py (2026-08-26, made
interactive 2026-08-27).

Started read-only (stdlib http.server, no new dependency, one HTML page
polling two JSON endpoints) - now also lets you submit a goal and approve a
push from the page itself, since watching-only turned out not to be enough
once real use started.

The interactive parts (`POST /api/submit`, `POST /api/push`) are a genuine
change in what this process can do, not a UI-only change: it now spawns
real orchestrator.run_task / chief.run_council calls (real git branches,
real model calls) and can push to GitHub. It's still bound to 127.0.0.1
only by default and still has NO AUTHENTICATION - anyone/anything that can
reach this port can now trigger real work against your repo, not just read
its history. That is a materially different risk than the read-only
version. It's accepted here because the same "no auth on localhost" trust
boundary already existed for the read-only dashboard and for the tmux
REPLs (agent-session.sh / chief-session.sh) themselves - this doesn't
introduce a new actor with different trust, it gives an existing one
(you, on your own machine) a second way in. Every push still requires an
explicit click (same human-approval-gate principle as the CLI's `--push`
flag) - nothing here auto-pushes.

Concurrency: a single in-process lock (RUN_LOCK) prevents the dashboard
from launching two overlapping runs against the same repo itself. It does
NOT know about a tmux REPL (agent-session.sh / chief-session.sh) also
running against the same repo at the same time - running both at once is
still on you to avoid, same caveat as everywhere else in this project.

How it sees anything: orchestrator.py's invoke_opencode() (used by both
run_task and chief.py's run_review) already writes every streamed model
event to `<repo>/.agent-dashboard/events.jsonl`, tagged with a run_id,
role (Engineer/QA/PM), and round. This process tails that file for the
live feed - no in-process pub/sub needed even for runs THIS process itself
spawns, since they write to the same file the same way a REPL-spawned run
would. History comes from the existing `agent-run-log.jsonl`.
"""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import chief
import orchestrator

HISTORY_FILE = "agent-run-log.jsonl"
MAX_HISTORY = 100

# Serializes runs this dashboard process itself launches against its one
# target repo - not a cross-process lock (see module docstring).
RUN_LOCK = threading.Lock()

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
  #active-banner {
    padding: 8px 18px; background: #1b1c22; border-bottom: 1px solid #2a2c34;
    font-size: 12px; color: #6a6d78; display: flex; align-items: center; gap: 8px;
  }
  #active-banner.live { color: #e5e7ee; }
  #active-banner .dot {
    width: 8px; height: 8px; border-radius: 50%; background: #3a3d47; flex-shrink: 0;
  }
  #active-banner.live .dot { background: #7bd99a; animation: pulse 1.2s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }
  .model-tag { font-size: 10px; color: #6a6d78; margin-left: 6px; }
  #layout { display: flex; height: calc(100vh - 105px); }
  #feed, #history { overflow-y: auto; padding: 14px 18px; }
  #feed { flex: 2; border-right: 1px solid #2a2c34; }
  #history { flex: 1; min-width: 280px; }
  h2 { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
       color: #7c7f8a; margin: 0 0 10px; }
  #submit-bar {
    padding: 10px 18px; border-bottom: 1px solid #2a2c34; background: #17181e;
    display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
  }
  #submit-bar input[type=text], #goal-input {
    flex: 1; min-width: 240px; background: #0f1014; border: 1px solid #2a2c34;
    color: #d8dade; padding: 7px 10px; border-radius: 4px; font-family: inherit; font-size: 13px;
  }
  #goal-input { resize: none; overflow-y: auto; max-height: 200px; line-height: 1.4; }
  #submit-bar select, #submit-bar button {
    background: #0f1014; border: 1px solid #2a2c34; color: #d8dade;
    padding: 7px 10px; border-radius: 4px; font-family: inherit; font-size: 12px; cursor: pointer;
  }
  #submit-bar button { background: #1c3352; border-color: #2c4a72; color: #7fb2ff; font-weight: 600; }
  #submit-bar button:disabled { opacity: 0.4; cursor: not-allowed; }
  #submit-bar label { font-size: 12px; color: #9a9dab; display: flex; align-items: center; gap: 4px; }
  #mode-caption {
    padding: 0 18px 10px; font-size: 11px; color: #6a6d78; background: #17181e;
    border-bottom: 1px solid #2a2c34; line-height: 1.5;
  }
  #model-bar {
    padding: 0 18px 10px; border-bottom: 1px solid #2a2c34; background: #17181e;
    display: flex; gap: 14px; flex-wrap: wrap;
  }
  .model-group { display: flex; gap: 12px; }
  .model-group label { font-size: 11px; color: #7c7f8a; display: flex; align-items: center; gap: 4px; }
  .model-group select {
    background: #0f1014; border: 1px solid #2a2c34; color: #d8dade;
    padding: 4px 6px; border-radius: 4px; font-family: inherit; font-size: 11px;
  }
  #custom-role-bar {
    padding: 8px 18px; border-bottom: 1px solid #2a2c34; background: #17181e;
    display: flex; gap: 8px; flex-wrap: wrap;
  }
  #custom-role-bar input[type=text] {
    background: #0f1014; border: 1px solid #2a2c34; color: #d8dade;
    padding: 5px 8px; border-radius: 4px; font-family: inherit; font-size: 11px;
  }
  #custom-role-name { flex: 0 0 220px; }
  #custom-role-instructions { flex: 1; min-width: 260px; }
  #custom-role-bar select {
    background: #0f1014; border: 1px solid #2a2c34; color: #d8dade;
    padding: 5px 6px; border-radius: 4px; font-family: inherit; font-size: 11px;
  }
  #submit-msg { padding: 0 18px; font-size: 12px; min-height: 18px; }
  #submit-msg.err { color: #ff8686; }
  #submit-msg.ok { color: #7bd99a; }
  .run { margin-bottom: 18px; border: 1px solid #23252c; border-radius: 6px; overflow: hidden; }
  .run-head { padding: 6px 10px; background: #1b1c22; font-size: 11px; color: #9a9dab;
              display: flex; justify-content: space-between; }
  .role-block { padding: 8px 10px; border-top: 1px solid #23252c; }
  .role-label { display: inline-block; font-weight: 700; font-size: 11px;
                padding: 1px 6px; border-radius: 3px; margin-bottom: 6px; }
  .role-Engineer { background: #1c3352; color: #7fb2ff; }
  .role-QA { background: #3a2e14; color: #e5b567; }
  .role-PM { background: #163a24; color: #7bd99a; }
  .role-Assistant { background: #2a2c34; color: #b7bac4; }
  .role-Custom { background: #2e1c3a; color: #c99fe0; }
  .timer { font-size: 10px; color: #6a6d78; margin-left: 6px; }
  .role-active { background: rgba(255,255,255,0.03); }
  .role-active .role-label { box-shadow: 0 0 0 1px currentColor; animation: pulse 1.2s infinite; }
  .line { padding: 1px 0; color: #b7bac4; white-space: pre-wrap; word-break: break-word; }
  .line.tool { color: #8b8ea0; }
  .line.err { color: #ff8686; }
  .end-status { margin-top: 4px; font-size: 11px; color: #7c7f8a; }
  .hist-item { padding: 8px 0; border-bottom: 1px solid #23252c; }
  .hist-status { font-weight: 700; font-size: 11px; padding: 1px 6px; border-radius: 3px; }
  .st-APPROVED, .st-COMMITTED, .st-PR_OPENED, .st-ANSWERED { background: #163a24; color: #7bd99a; }
  .st-BLOCKED_GATE_FAIL, .st-PR_FAILED, .st-NEEDS_HUMAN, .st-REVISION_FAILED, .st-ENGINEER_BLOCKED { background: #3a1616; color: #ff8686; }
  .st-ESCALATED, .st-REFUSED_DIRTY { background: #3a2e14; color: #e5b567; }
  .st-NO_CHANGES, .st-ENGINEER_NO_CHANGES { background: #23252c; color: #9a9dab; }
  .hist-task { color: #d8dade; margin-top: 3px; }
  .hist-time { color: #6a6d78; font-size: 11px; margin-top: 3px; }
  .push-btn {
    margin-top: 5px; background: #1c3352; border: 1px solid #2c4a72; color: #7fb2ff;
    padding: 3px 8px; border-radius: 3px; font-size: 11px; cursor: pointer; font-family: inherit;
  }
  .push-btn:disabled { opacity: 0.4; cursor: not-allowed; }
  #empty { color: #6a6d78; padding: 20px 0; }
</style>
</head>
<body>
<header>
  <h1>Chief of Staff - live monitor</h1>
  <span class="repo" id="repo-path"></span>
</header>
<div id="submit-bar">
  <select id="mode-select">
    <option value="agent">Implement only (fast, no review)</option>
    <option value="chief">Implement + review (slower, QA/PM must approve)</option>
  </select>
  <textarea id="goal-input" rows="1" placeholder="Describe the task or goal... (Enter to submit, Shift+Enter for a new line)"></textarea>
  <label title="Once QA/PM (and any custom reviewer) all say APPROVE, push the branch to GitHub and open a draft PR automatically - the same thing the 'Push + open PR' button in History does, just pre-approved before you see the result. Leave unchecked to review it yourself first and push manually.">
    <input type="checkbox" id="push-checkbox"> auto-push when approved
  </label>
  <button id="submit-btn">Submit</button>
</div>
<div id="mode-caption">
  Both verify success by git diff, never by the model's own claim. "Implement only" is one pass -
  you review the result yourself. "Implement + review" adds two more passes (QA, then PM) that
  must both say APPROVE before it's done, with one bounded revision round if either objects -
  slower, but catches problems before you'd otherwise have to notice them.
</div>
<div id="model-bar">
  <div id="model-agent" class="model-group">
    <label>model: <select id="model-agent-select">__MODEL_OPTIONS_FAST__</select></label>
  </div>
  <div id="model-chief" class="model-group" style="display:none">
    <label>Engineer: <select id="model-eng-select">__MODEL_OPTIONS_FAST__</select></label>
    <label>QA: <select id="model-qa-select">__MODEL_OPTIONS_RELIABLE__</select></label>
    <label>PM: <select id="model-pm-select">__MODEL_OPTIONS_RELIABLE__</select></label>
  </div>
</div>
<div id="custom-role-bar" style="display:none">
  <input type="text" id="custom-role-name" placeholder="Optional extra reviewer role, e.g. UI/UX Designer">
  <input type="text" id="custom-role-instructions" placeholder="What should they specifically check? e.g. visual consistency, accessibility, design system adherence">
  <select id="model-custom-select">__MODEL_OPTIONS_RELIABLE__</select>
</div>
<div id="submit-msg"></div>
<div id="active-banner"><span class="dot"></span><span id="active-text">Idle - nothing running</span></div>
<div id="layout">
  <div id="feed"><h2>Live feed</h2><div id="runs"></div></div>
  <div id="history"><h2>History</h2><div id="hist-list"><div id="empty">No runs yet.</div></div></div>
</div>
<script>
let offset = 0;
const runs = {}; // run_id -> { el, roles: { role -> {el, started, ended} } }
const activeRoles = new Map(); // key -> { role, model }

function formatElapsed(ms) {
  const s = Math.floor(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  return m + 'm ' + (s % 60) + 's';
}

function updateBanner() {
  const banner = document.getElementById('active-banner');
  const text = document.getElementById('active-text');
  const submitBtn = document.getElementById('submit-btn');
  if (activeRoles.size === 0) {
    banner.classList.remove('live');
    text.textContent = 'Idle - nothing running';
    submitBtn.disabled = false;
    return;
  }
  banner.classList.add('live');
  const now = Date.now();
  const parts = [...activeRoles.values()].map(v =>
    v.role + ' (' + (v.model || 'unknown model') + ') - ' + formatElapsed(now - v.startedAt));
  text.textContent = 'Running: ' + parts.join(', ');
  submitBtn.disabled = true;
}

// Ticks every second so elapsed time updates live without waiting on the
// next /api/events poll - direct user request ("I want a timer so I know
// how long it's been running").
setInterval(() => {
  if (activeRoles.size === 0) return;
  const now = Date.now();
  for (const v of activeRoles.values()) {
    if (v.rec && v.rec.timerEl) v.rec.timerEl.textContent = formatElapsed(now - v.startedAt);
  }
  updateBanner();
}, 1000);

const KNOWN_ROLES = ['Engineer', 'QA', 'PM', 'Assistant'];

function roleBlock(runEl, run_id, role) {
  const key = run_id + ':' + role;
  if (runs[run_id].roles[key]) return runs[run_id].roles[key];
  // A custom reviewer's name (e.g. "UI/UX Designer") isn't a valid CSS
  // class token as-is (spaces, slashes) - fall back to one shared style
  // for anything outside the 3 fixed roles, keep the real name as the
  // visible label text.
  const roleClass = KNOWN_ROLES.includes(role) ? 'role-' + role : 'role-Custom';
  const el = document.createElement('div');
  el.className = 'role-block ' + roleClass;
  el.innerHTML = '<span class="role-label ' + roleClass + '">' + role + '</span>' +
                 '<span class="model-tag"></span><span class="timer"></span><div class="lines"></div>';
  runEl.appendChild(el);
  const rec = { el, lines: el.querySelector('.lines'), modelTag: el.querySelector('.model-tag'),
                timerEl: el.querySelector('.timer'), startedAt: null, ended: false };
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
  const roundEl = run.el.querySelector('.round');
  roundEl.textContent = 'attempt ' + (e.round || 1) + ' of ≤2';
  roundEl.title = 'Attempt 1: first try. Attempt 2 only happens in "Implement + review" '
                + 'mode, if QA/PM (or a custom reviewer) request changes - never more than 2.';
  const activeKey = e.run_id + ':' + e.role;
  if (e.kind === 'start') {
    rec.el.classList.add('role-active');
    rec.startedAt = Date.now();
    if (rec.modelTag) rec.modelTag.textContent = e.model ? ('· ' + e.model + ' · running') : '';
    activeRoles.set(activeKey, { role: e.role, model: e.model, startedAt: rec.startedAt, rec });
    updateBanner();
    addLine(rec, 'task: ' + e.task, 'tool');
  } else if (e.kind === 'opencode_event') {
    const ev = e.event || {};
    const part = ev.part || {};
    if (ev.type === 'text') {
      const t = (part.text || '').trim();
      if (t) addLine(rec, t.slice(0, 2000));
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
    activeRoles.delete(activeKey);
    updateBanner();
    if (rec.modelTag) rec.modelTag.textContent = rec.modelTag.textContent.replace('· running', '· done');
    const elapsed = rec.startedAt ? ' (' + formatElapsed(Date.now() - rec.startedAt) + ')' : '';
    if (rec.timerEl) rec.timerEl.textContent = '';
    const d = document.createElement('div');
    d.className = 'end-status';
    d.textContent = e.role + ' finished: ' + e.status + elapsed;
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

function isPushEligible(e) {
  // COMMITTED = a plain orchestrator.py run that passed the gate but wasn't
  // pushed; a chief_report of APPROVED with no pr_result yet is the same
  // state for a council run. Either way: committed, gate-passed, not pushed.
  if (e.pr_result) return false;
  if (e.status === 'COMMITTED') return true;
  if (e.chief_report && e.chief_report.status === 'APPROVED' && !e.chief_report.pr_result) return true;
  return false;
}

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
        const branch = (e.chief_report && e.chief_report.branch) || e.branch || '';
        const task = branch || e.task || '';
        const pushBtn = isPushEligible(e)
          ? '<button class="push-btn" data-branch="' + branch + '" data-task="' +
            (e.task || '').replace(/"/g, '&quot;') + '">Push + open PR</button>'
          : '';
        return '<div class="hist-item"><span class="hist-status ' + statusClass(status) + '">' + status + '</span>' +
               '<div class="hist-task">' + task + '</div>' +
               '<div class="hist-time">' + (e.timestamp || '') + '</div>' + pushBtn + '</div>';
      }).join('');
    }
  } catch (e) { /* retry next tick */ }
  setTimeout(pollHistory, 3000);
}

function showMsg(text, cls) {
  const el = document.getElementById('submit-msg');
  el.textContent = text;
  el.className = cls || '';
}

document.getElementById('mode-select').addEventListener('change', () => {
  const mode = document.getElementById('mode-select').value;
  document.getElementById('model-agent').style.display = mode === 'agent' ? 'flex' : 'none';
  document.getElementById('model-chief').style.display = mode === 'chief' ? 'flex' : 'none';
  document.getElementById('custom-role-bar').style.display = mode === 'chief' ? 'flex' : 'none';
});

const goalInput = document.getElementById('goal-input');
// Auto-grow the textarea as multi-line input is typed (Shift+Enter).
goalInput.addEventListener('input', () => {
  goalInput.style.height = 'auto';
  goalInput.style.height = Math.min(goalInput.scrollHeight, 200) + 'px';
});
goalInput.addEventListener('keydown', (ev) => {
  if (ev.key === 'Enter' && !ev.shiftKey) {
    ev.preventDefault();
    document.getElementById('submit-btn').click();
  }
  // Shift+Enter: no handler needed - textarea's own default behavior
  // (insert a newline) already does exactly this.
});

document.getElementById('submit-btn').addEventListener('click', async () => {
  const goal = document.getElementById('goal-input').value.trim();
  const mode = document.getElementById('mode-select').value;
  const push = document.getElementById('push-checkbox').checked;
  if (!goal) { showMsg('Enter a goal first.', 'err'); return; }
  document.getElementById('submit-btn').disabled = true;
  const payload = {goal, mode, push};
  if (mode === 'chief') {
    payload.engineer_model = document.getElementById('model-eng-select').value;
    payload.qa_model = document.getElementById('model-qa-select').value;
    payload.pm_model = document.getElementById('model-pm-select').value;
    const customName = document.getElementById('custom-role-name').value.trim();
    const customInstructions = document.getElementById('custom-role-instructions').value.trim();
    if (customName && customInstructions) {
      payload.extra_reviewers = [{
        name: customName, instructions: customInstructions,
        model: document.getElementById('model-custom-select').value,
      }];
    }
  } else {
    payload.model = document.getElementById('model-agent-select').value;
  }
  try {
    const res = await fetch('/api/submit', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) { showMsg('Error: ' + (data.error || res.status), 'err'); document.getElementById('submit-btn').disabled = false; return; }
    showMsg('Started: ' + goal, 'ok');
    document.getElementById('goal-input').value = '';
  } catch (e) {
    showMsg('Request failed: ' + e, 'err');
    document.getElementById('submit-btn').disabled = false;
  }
});

document.getElementById('hist-list').addEventListener('click', async (ev) => {
  const btn = ev.target.closest('.push-btn');
  if (!btn) return;
  btn.disabled = true;
  btn.textContent = 'Pushing...';
  try {
    const res = await fetch('/api/push', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({branch: btn.dataset.branch, task: btn.dataset.task}),
    });
    const data = await res.json();
    if (!res.ok) { showMsg('Push error: ' + (data.error || res.status), 'err'); btn.disabled = false; btn.textContent = 'Push + open PR'; return; }
    showMsg('Push started for ' + btn.dataset.branch, 'ok');
    btn.textContent = 'Pushing (watch live feed)...';
  } catch (e) {
    showMsg('Push request failed: ' + e, 'err');
    btn.disabled = false;
    btn.textContent = 'Push + open PR';
  }
});

document.getElementById('repo-path').textContent = window.location.search.slice(1) ? '' : '';
pollEvents();
pollHistory();
</script>
</body>
</html>
"""


def _model_options_html(default: str) -> str:
    """Builds <option> tags from orchestrator.AVAILABLE_MODELS - adding a
    model there (e.g. a newly-pulled local one) makes it show up in every
    dropdown at once, instead of needing 5 hardcoded option lists kept in
    sync by hand."""
    labels = {orchestrator.MODEL_INTERACTIVE: "fast", orchestrator.MODEL_QUEUED: "reliable"}
    parts = []
    for m in orchestrator.AVAILABLE_MODELS:
        label = labels.get(m, "extra")
        selected = " selected" if m == default else ""
        parts.append(f'<option value="{m}"{selected}>{m} ({label})</option>')
    return "".join(parts)


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

    def _read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            body = (PAGE
                    .replace("__MODEL_OPTIONS_FAST__", _model_options_html(orchestrator.MODEL_INTERACTIVE))
                    .replace("__MODEL_OPTIONS_RELIABLE__", _model_options_html(orchestrator.MODEL_QUEUED))
                    ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path == "/api/events":
            qs = parse_qs(parsed.query)
            offset = int(qs.get("offset", ["0"])[0])
            events_path = self.repo / orchestrator.DASHBOARD_DIR / orchestrator.DASHBOARD_EVENTS_FILE
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

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            body = self._read_json_body()
        except json.JSONDecodeError:
            self._json({"error": "invalid JSON body"}, status=400)
            return

        if parsed.path == "/api/submit":
            self._handle_submit(body)
            return
        if parsed.path == "/api/push":
            self._handle_push(body)
            return
        self._json({"error": "not found"}, status=404)

    def _handle_submit(self, body: dict) -> None:
        goal = (body.get("goal") or "").strip()
        mode = body.get("mode", "agent")
        push = bool(body.get("push", False))
        if not goal:
            self._json({"error": "goal is required"}, status=400)
            return
        if mode not in ("agent", "chief"):
            self._json({"error": "mode must be 'agent' or 'chief'"}, status=400)
            return

        # Per-role model override (2026-08-27, inspired by OpenExecutive's
        # per-agent model dropdown) - validated against the two models this
        # project actually knows how to run, so a bad value fails fast here
        # with a clear error instead of a confusing downstream OpenCode one.
        known_models = set(orchestrator.AVAILABLE_MODELS)
        model_fields = (["model"] if mode == "agent"
                         else ["engineer_model", "qa_model", "pm_model"])
        models = {}
        for field in model_fields:
            value = body.get(field) or orchestrator.MODEL_INTERACTIVE
            if value not in known_models:
                self._json({"error": f"{field} must be one of {sorted(known_models)}"}, status=400)
                return
            models[field] = value

        # Optional user-defined reviewer roles (2026-08-27), chief mode only.
        extra_reviewers = []
        if mode == "chief":
            for reviewer in body.get("extra_reviewers") or []:
                name = (reviewer.get("name") or "").strip()
                instructions = (reviewer.get("instructions") or "").strip()
                if not name or not instructions:
                    self._json({"error": "each extra_reviewers entry needs both "
                                          "name and instructions"}, status=400)
                    return
                reviewer_model = reviewer.get("model") or orchestrator.MODEL_INTERACTIVE
                if reviewer_model not in known_models:
                    self._json({"error": f"extra_reviewers model must be one of {sorted(known_models)}"},
                                status=400)
                    return
                extra_reviewers.append({"name": name, "instructions": instructions, "model": reviewer_model})

        if not RUN_LOCK.acquire(blocking=False):
            self._json({"error": "another run is already in progress against "
                                  "this repo - wait for it to finish"}, status=409)
            return

        repo = self.repo

        def worker():
            try:
                if mode == "chief":
                    chief.run_council(repo, goal, push=push,
                                       engineer_model=models["engineer_model"],
                                       qa_model=models["qa_model"],
                                       pm_model=models["pm_model"],
                                       extra_reviewers=extra_reviewers)
                else:
                    orchestrator.handle_goal(repo, goal, push=push, model=models["model"])
            except orchestrator.MissingExecutable as e:
                orchestrator.emit_dashboard_event(repo, {
                    "run_id": "dashboard-error", "role": "System", "round": 1,
                    "kind": "opencode_event",
                    "event": {"type": "text", "part": {"text": f"ERROR: {e}"}},
                })
            except Exception as e:  # noqa: BLE001 - a thread crash must not vanish silently
                orchestrator.emit_dashboard_event(repo, {
                    "run_id": "dashboard-error", "role": "System", "round": 1,
                    "kind": "opencode_event",
                    "event": {"type": "text", "part": {"text": f"Unexpected error: {e}"}},
                })
            finally:
                RUN_LOCK.release()

        threading.Thread(target=worker, daemon=True).start()
        self._json({"status": "started", "mode": mode, "goal": goal, "push": push,
                    "extra_reviewers": extra_reviewers, **models})

    def _handle_push(self, body: dict) -> None:
        branch = (body.get("branch") or "").strip()
        task = (body.get("task") or "").strip() or branch
        if not branch:
            self._json({"error": "branch is required"}, status=400)
            return

        repo = self.repo
        if not orchestrator.branch_exists(repo, branch):
            self._json({"error": f"branch {branch!r} does not exist"}, status=400)
            return

        if not RUN_LOCK.acquire(blocking=False):
            self._json({"error": "another run is in progress - wait for it "
                                  "to finish before pushing"}, status=409)
            return

        def worker():
            try:
                orchestrator.run(["git", "checkout", branch], cwd=repo, check=True)
                # Re-verify the gate fresh rather than trust a result from
                # whenever the run originally finished - repo state may have
                # changed since, and this project never trusts stale state.
                gate = orchestrator.run_test_gate(repo)
                result = orchestrator.push_and_open_pr(repo, branch, task, gate)
                orchestrator.append_log(repo, {
                    "task": task, "branch": branch,
                    "status": "PR_OPENED" if result["status"] == "OPENED" else "PR_FAILED",
                    "gate_status": gate["status"], "pr_result": result,
                })
            except orchestrator.MissingExecutable as e:
                orchestrator.emit_dashboard_event(repo, {
                    "run_id": "dashboard-error", "role": "System", "round": 1,
                    "kind": "opencode_event",
                    "event": {"type": "text", "part": {"text": f"ERROR: {e}"}},
                })
            except Exception as e:  # noqa: BLE001
                orchestrator.emit_dashboard_event(repo, {
                    "run_id": "dashboard-error", "role": "System", "round": 1,
                    "kind": "opencode_event",
                    "event": {"type": "text", "part": {"text": f"Push failed: {e}"}},
                })
            finally:
                RUN_LOCK.release()

        threading.Thread(target=worker, daemon=True).start()
        self._json({"status": "started", "branch": branch})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("repo", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1",
                         help="Bind address. Default is localhost-only - "
                              "there is no auth, and this dashboard can now "
                              "trigger real runs and pushes, so opening it "
                              "to 0.0.0.0 is a deliberate opt-in with real "
                              "consequences, not the default.")
    args = parser.parse_args()

    Handler.repo = args.repo.resolve()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Dashboard for {Handler.repo} - http://{args.host}:{args.port}")
    print("Interactive: you can submit goals and approve pushes from this "
          "page now. No auth - anything that can reach this port can too.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
