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

`POST /api/daemon/start` / `POST /api/daemon/stop` (2026-09-03, direct user
request: "I know it's running on tmux, but I want UI dashboard on
browser") are a further step up in what this process can do: it now shells
out to the `tmux` binary to spawn or kill a whole separate OS process (a
`daemon-<repo>` tmux session running `chief.py --daemon`), not just spawn
an in-process thread like submit/push do. Same no-auth trust boundary as
everywhere else in this file, same click-triggered nothing-automatic
principle - but worth naming explicitly since "start a background process
on your machine" is a bigger action than "run a git branch operation".
Start refuses (409) if orchestrator.daemon_is_alive() already says yes;
stop defaults to the graceful STOP-flag signal (finishes whatever item is
currently running first) with a `force` option that kills the tmux session
outright - safe by the same D6 crash-resume logic that already handles a
daemon dying mid-task, not a new risk.

How it sees anything: orchestrator.py's invoke_opencode() (used by both
run_task and chief.py's run_review) already writes every streamed model
event to `<repo>/.agent-dashboard/events.jsonl`, tagged with a run_id,
role (Engineer/QA/PM), and round. This process tails that file for the
live feed - no in-process pub/sub needed even for runs THIS process itself
spawns, since they write to the same file the same way a REPL-spawned run
would. History comes from the existing `agent-run-log.jsonl`.
"""

import argparse
import html
import json
import shlex
import subprocess
import threading
from datetime import datetime, timezone
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
  header .repo { color: #7c7f8a; font-size: 12px; display: flex; align-items: center; gap: 8px; }
  #workspace-toggle {
    background: none; border: 1px solid #2a2c34; color: #7c7f8a;
    padding: 2px 8px; border-radius: 10px; font-size: 10px; cursor: pointer; font-family: inherit;
  }
  #workspace-toggle:hover { color: #9a9dab; border-color: #3a3d47; }
  #workspace-bar {
    padding: 8px 18px; border-bottom: 1px solid #2a2c34; background: #17181e;
    display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
  }
  #workspace-label { font-size: 11px; color: #7c7f8a; }
  #workspace-options { display: flex; gap: 6px; flex-wrap: wrap; }
  #workspace-loading { font-size: 11px; color: #6a6d78; }
  #workspace-empty { font-size: 11px; color: #6a6d78; font-style: italic; }
  .workspace-chip {
    background: #0f1014; border: 1px solid #2a2c34; color: #9a9dab;
    padding: 4px 10px; border-radius: 12px; font-family: inherit; font-size: 11px; cursor: pointer;
  }
  .workspace-chip:hover { border-color: #3a3d47; color: #d8dade; }
  #workspace-new-btn {
    background: #1c3352; border: 1px solid #2c4a72; color: #7fb2ff;
    padding: 5px 12px; border-radius: 12px; font-family: inherit; font-size: 11px; cursor: pointer;
  }
  #workspace-msg { font-size: 11px; }
  #workspace-msg.err { color: #ff8686; }
  #workspace-msg.ok { color: #7bd99a; }
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
  #feed, #history, #queue { overflow-y: auto; padding: 14px 18px; }
  #feed { flex: 2; border-right: 1px solid #2a2c34; }
  #history { flex: 1; min-width: 280px; border-right: 1px solid #2a2c34; transition: min-width 0.15s, flex 0.15s; }
  #history.collapsed { flex: 0 0 auto; min-width: 0; width: 140px; overflow: hidden; }
  #history.collapsed #hist-list { display: none; }
  #queue { flex: 1; min-width: 280px; transition: min-width 0.15s, flex 0.15s; }
  #queue.collapsed { flex: 0 0 auto; min-width: 0; width: 140px; overflow: hidden; }
  #queue.collapsed #queue-body { display: none; }
  h2 { font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
       color: #7c7f8a; margin: 0 0 10px; }
  #history-toggle, #queue-toggle { cursor: pointer; user-select: none; display: flex;
                     align-items: center; gap: 4px; }
  #history-toggle:hover, #queue-toggle:hover { color: #9a9dab; }
  #daemon-status { font-size: 11px; padding: 6px 8px; border-radius: 4px;
                    margin-bottom: 10px; background: #23252c; color: #9a9dab; }
  #daemon-status.alive { background: #163a24; color: #7bd99a; }
  #daemon-status.stale { background: #3a1616; color: #ff8686; }
  #daemon-controls { display: flex; gap: 6px; align-items: center; margin-bottom: 10px; flex-wrap: wrap; }
  #daemon-controls select, #daemon-controls button {
    background: #0f1014; border: 1px solid #2a2c34; color: #d8dade;
    padding: 6px 10px; border-radius: 4px; font-family: inherit; font-size: 12px; cursor: pointer;
  }
  #daemon-start-btn { border-color: #2c4a72; color: #7fb2ff; }
  #daemon-stop-btn, #daemon-force-stop-btn { border-color: #4a2020; color: #ff8686; }
  #queue-tasks-input { width: 100%; box-sizing: border-box; background: #0f1014;
                        border: 1px solid #2a2c34; color: #d8dade; padding: 7px 10px;
                        border-radius: 4px; font-family: inherit; font-size: 12px;
                        resize: vertical; min-height: 50px; margin-bottom: 6px; }
  #queue-add-btn { background: #1c3352; border: 1px solid #2c4a72; color: #7fb2ff;
                    padding: 6px 10px; border-radius: 4px; font-size: 12px;
                    cursor: pointer; font-family: inherit; }
  #queue-msg { font-size: 11px; margin: 6px 0; min-height: 14px; }
  #queue-msg.err { color: #ff8686; }
  #queue-msg.ok { color: #7bd99a; }
  .queue-item { padding: 8px 0; border-bottom: 1px solid #23252c; }
  .queue-task { color: #d8dade; margin-top: 3px; font-size: 12px; }
  .st-PENDING { background: #23252c; color: #9a9dab; }
  .st-RUNNING { background: #1c3352; color: #7fb2ff; }
  .st-FAILED { background: #3a1616; color: #ff8686; }
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
  }
  #role-presets { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-bottom: 6px; }
  #role-presets-label { font-size: 11px; color: #7c7f8a; margin-right: 2px; }
  .role-chip {
    background: #0f1014; border: 1px solid #2a2c34; color: #9a9dab;
    padding: 4px 9px; border-radius: 12px; font-family: inherit; font-size: 11px;
    cursor: pointer;
  }
  .role-chip:hover { border-color: #3a3d47; }
  .role-chip.active { background: #2e1c3a; border-color: #4a2e5c; color: #c99fe0; }
  #custom-role-fields { display: flex; gap: 8px; flex-wrap: wrap; }
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
  <span class="repo">
    <span id="repo-path">__REPO_PATH__</span>
    <button type="button" id="workspace-toggle">change</button>
  </span>
</header>
<div id="workspace-bar" style="display:none">
  <span id="workspace-label" title="Most recently active sibling projects in the same parent folder">switch to (recent):</span>
  <span id="workspace-options"><span id="workspace-loading">loading...</span></span>
  <button type="button" id="workspace-new-btn">+ New workspace</button>
  <span id="workspace-msg"></span>
</div>
<div id="submit-bar">
  <select id="mode-select">
    <option value="chief">General (team review: Engineer + QA + PM)</option>
    <option value="agent">Build (fast, no review)</option>
  </select>
  <textarea id="goal-input" rows="1" placeholder="Describe the task or goal... (Enter to submit, Shift+Enter for a new line)"></textarea>
  <label title="Once QA/PM (and any custom reviewer) all say APPROVE, push the branch to GitHub and open a draft PR automatically - the same thing the 'Push + open PR' button in History does, just pre-approved before you see the result. Leave unchecked to review it yourself first and push manually.">
    <input type="checkbox" id="push-checkbox"> auto-push when approved
  </label>
  <button id="submit-btn">Submit</button>
</div>
<div id="mode-caption">
  Both verify success by git diff, never by the model's own claim, and both answer plain
  questions directly (no branch, no commit) instead of forcing them through a build. "Build" is
  one pass - you review the result yourself. "General" adds two more passes (QA, then PM) that
  must both say APPROVE before a build is done, with one bounded revision round if either
  objects - slower, but catches problems before you'd otherwise have to notice them.
</div>
<div id="model-bar">
  <div id="model-agent" class="model-group" style="display:none">
    <label>model: <select id="model-agent-select">__MODEL_OPTIONS_FAST__</select></label>
  </div>
  <div id="model-chief" class="model-group">
    <label>Engineer: <select id="model-eng-select">__MODEL_OPTIONS_FAST__</select></label>
    <label>QA: <select id="model-qa-select">__MODEL_OPTIONS_RELIABLE__</select></label>
    <label>PM: <select id="model-pm-select">__MODEL_OPTIONS_RELIABLE__</select></label>
  </div>
</div>
<div id="custom-role-bar">
  <div id="role-presets">
    <span id="role-presets-label">extra reviewers:</span>
    <button type="button" class="role-chip" data-role="uiux">+ UI/UX Designer</button>
    <button type="button" class="role-chip" data-role="security">+ Security</button>
    <button type="button" class="role-chip" data-role="performance">+ Performance</button>
    <button type="button" class="role-chip" data-role="accessibility">+ Accessibility</button>
    <button type="button" class="role-chip" data-role="docs">+ Docs</button>
    <select id="model-custom-select" title="Model used for all extra reviewers (presets and custom)">__MODEL_OPTIONS_RELIABLE__</select>
  </div>
  <div id="custom-role-fields">
    <input type="text" id="custom-role-name" placeholder="Or a custom role name, e.g. Data Privacy Reviewer">
    <input type="text" id="custom-role-instructions" placeholder="What should they specifically check?">
  </div>
</div>
<div id="submit-msg"></div>
<div id="active-banner"><span class="dot"></span><span id="active-text">Idle - nothing running</span></div>
<div id="layout">
  <div id="feed"><h2>Live feed</h2><div id="runs"></div></div>
  <div id="history">
    <h2 id="history-toggle">History <span id="history-arrow">▾</span></h2>
    <div id="hist-list"><div id="empty">No runs yet.</div></div>
  </div>
  <div id="queue">
    <h2 id="queue-toggle">24/7 Queue <span id="queue-arrow">▾</span></h2>
    <div id="queue-body">
      <div id="daemon-status">checking daemon...</div>
      <div id="daemon-controls">
        <select id="daemon-model-select" title="Model used for all three roles (Engineer/QA/PM) when the daemon starts">__MODEL_OPTIONS_RELIABLE__</select>
        <button type="button" id="daemon-start-btn">Start daemon</button>
        <button type="button" id="daemon-stop-btn" style="display:none">Stop (after current item)</button>
        <button type="button" id="daemon-force-stop-btn" style="display:none">Force stop</button>
      </div>
      <textarea id="queue-tasks-input" rows="3" placeholder="One task per line - added to the queue for the daemon (chief.py --daemon / daemon-session.sh) to work through, not run by this page."></textarea>
      <button type="button" id="queue-add-btn">+ Add to queue</button>
      <div id="queue-msg"></div>
      <div id="queue-list"><div id="queue-empty">Queue is empty.</div></div>
    </div>
  </div>
</div>
<script>
// Templatized reviewer roles (direct user request: "don't ask users to put
// all the details of what roles agents need... make presets... allow users
// to create their own team with only a few clicks"). Each preset already
// has real instructions written - clicking a chip adds it to the team,
// no typing required. The custom-role fields below stay as a fallback for
// anything not covered by a preset, not the primary way to add a role.
const ROLE_PRESETS = {
  uiux: { name: 'UI/UX Designer',
    instructions: 'Check that the diff maintains visual consistency, accessibility, and follows the existing design system. Flag anything that would look inconsistent or confusing to an end user.' },
  security: { name: 'Security Reviewer',
    instructions: 'Check for security issues: injection vulnerabilities, hardcoded secrets, unsafe deserialization, missing input validation, and anything that could be exploited.' },
  performance: { name: 'Performance Reviewer',
    instructions: 'Check for performance issues: unnecessary loops, N+1 queries, unbounded memory growth, blocking calls that should be async, and anything that would slow down at scale.' },
  accessibility: { name: 'Accessibility Reviewer',
    instructions: 'Check that any UI changes are accessible: proper ARIA labels, keyboard navigation, sufficient color contrast, and screen-reader compatibility.' },
  docs: { name: 'Docs Reviewer',
    instructions: 'Check that any new public functions, classes, or APIs have clear documentation explaining WHY, not just what, and that the change would be understandable to someone unfamiliar with this code.' },
};
const selectedPresetRoles = new Set();
document.querySelectorAll('.role-chip').forEach(chip => {
  chip.addEventListener('click', () => {
    const key = chip.dataset.role;
    if (selectedPresetRoles.has(key)) {
      selectedPresetRoles.delete(key);
      chip.classList.remove('active');
    } else {
      selectedPresetRoles.add(key);
      chip.classList.add('active');
    }
  });
});

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
  roundEl.title = 'Attempt 1: first try. Attempt 2 only happens in "General" mode, if '
                + 'QA/PM (or a custom reviewer) request changes - never more than 2.';
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

// 24/7 daemon queue panel (direct user request: "run my local model 24/7...
// give it a to-do list and it does one by one... never rest until it
// absolutely requires human review"). The daemon (chief.py --daemon) is a
// SEPARATE process from this dashboard - this page only reads the same
// .agent-queue files and the daemon's heartbeat file, and can add new
// tasks to the queue, but never runs the queue itself.
function daemonStatusText(daemon) {
  if (!daemon || !daemon.alive) return 'Daemon: not running. Start it with ./daemon-session.sh <repo>.';
  if (daemon.state === 'working') return 'Daemon: running - working on: ' + (daemon.current_task || '?');
  if (daemon.state === 'idle') return 'Daemon: running - idle, watching for new tasks.';
  if (daemon.state === 'stopped') return 'Daemon: stopped' + (daemon.error ? ' (' + daemon.error + ')' : '') + '.';
  return 'Daemon: running.';
}

async function pollQueue() {
  try {
    const res = await fetch('/api/queue');
    const data = await res.json();
    const statusEl = document.getElementById('daemon-status');
    statusEl.textContent = daemonStatusText(data.daemon);
    const alive = !!(data.daemon && data.daemon.alive);
    statusEl.className = alive ? 'alive' : 'stale';
    document.getElementById('daemon-model-select').style.display = alive ? 'none' : 'inline-block';
    document.getElementById('daemon-start-btn').style.display = alive ? 'none' : 'inline-block';
    document.getElementById('daemon-stop-btn').style.display = alive ? 'inline-block' : 'none';
    document.getElementById('daemon-force-stop-btn').style.display = alive ? 'inline-block' : 'none';

    const list = document.getElementById('queue-list');
    if (!data.entries || data.entries.length === 0) {
      list.innerHTML = '<div id="queue-empty">Queue is empty.</div>';
    } else {
      list.innerHTML = data.entries.map(e => {
        const badge = e.result || (e.status || 'UNKNOWN').toUpperCase();
        const extra = e.error ? ' - ' + e.error : '';
        return '<div class="queue-item"><span class="hist-status ' + statusClass(badge) + '">' + badge + '</span>' +
               '<div class="queue-task">' + (e.task || '') + extra + '</div></div>';
      }).join('');
    }
  } catch (e) { /* retry next tick */ }
  setTimeout(pollQueue, 3000);
}

document.getElementById('queue-add-btn').addEventListener('click', async () => {
  const input = document.getElementById('queue-tasks-input');
  const msg = document.getElementById('queue-msg');
  const tasks = input.value;
  if (!tasks.trim()) { msg.textContent = 'Enter at least one task first.'; msg.className = 'err'; return; }
  try {
    const res = await fetch('/api/queue', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({tasks}),
    });
    const data = await res.json();
    if (!res.ok) { msg.textContent = 'Error: ' + (data.error || res.status); msg.className = 'err'; return; }
    msg.textContent = 'Queued ' + data.count + ' task(s).'; msg.className = 'ok';
    input.value = '';
    pollQueue();
  } catch (e) {
    msg.textContent = 'Request failed: ' + e; msg.className = 'err';
  }
});

async function daemonControl(url, payload, pendingText) {
  const msg = document.getElementById('queue-msg');
  msg.textContent = pendingText; msg.className = '';
  try {
    const res = await fetch(url, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) { msg.textContent = 'Error: ' + (data.error || res.status); msg.className = 'err'; return; }
    msg.textContent = 'Status: ' + data.status; msg.className = 'ok';
  } catch (e) {
    msg.textContent = 'Request failed: ' + e; msg.className = 'err';
  }
  pollQueue();
}

document.getElementById('daemon-start-btn').addEventListener('click', () => {
  const model = document.getElementById('daemon-model-select').value;
  daemonControl('/api/daemon/start', {model}, 'Starting daemon...');
});
document.getElementById('daemon-stop-btn').addEventListener('click', () => {
  daemonControl('/api/daemon/stop', {}, 'Stop requested - finishing the current item first...');
});
document.getElementById('daemon-force-stop-btn').addEventListener('click', () => {
  daemonControl('/api/daemon/stop', {force: true}, 'Force stopping...');
});

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
    const extraModel = document.getElementById('model-custom-select').value;
    const extraReviewers = [...selectedPresetRoles].map(key => ({
      name: ROLE_PRESETS[key].name, instructions: ROLE_PRESETS[key].instructions, model: extraModel,
    }));
    const customName = document.getElementById('custom-role-name').value.trim();
    const customInstructions = document.getElementById('custom-role-instructions').value.trim();
    if (customName && customInstructions) {
      extraReviewers.push({ name: customName, instructions: customInstructions, model: extraModel });
    }
    if (extraReviewers.length) payload.extra_reviewers = extraReviewers;
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

// Collapsible history panel (direct user request - "doesn't add a lot of
// value, user might want to hide it"). Persisted per-browser via
// localStorage so the choice survives a reload; wrapped in try/catch since
// storage access can throw in some contexts (private windows etc.) and
// this is a pure convenience, never worth breaking the page over.
(function initHistoryToggle() {
  const historyEl = document.getElementById('history');
  const arrow = document.getElementById('history-arrow');
  let collapsed = false;
  try { collapsed = localStorage.getItem('historyCollapsed') === '1'; } catch (e) {}
  function apply() {
    historyEl.classList.toggle('collapsed', collapsed);
    arrow.textContent = collapsed ? '▸' : '▾';
  }
  apply();
  document.getElementById('history-toggle').addEventListener('click', () => {
    collapsed = !collapsed;
    apply();
    try { localStorage.setItem('historyCollapsed', collapsed ? '1' : '0'); } catch (e) {}
  });
})();

(function initQueueToggle() {
  const queueEl = document.getElementById('queue');
  const arrow = document.getElementById('queue-arrow');
  let collapsed = false;
  try { collapsed = localStorage.getItem('queueCollapsed') === '1'; } catch (e) {}
  function apply() {
    queueEl.classList.toggle('collapsed', collapsed);
    arrow.textContent = collapsed ? '▸' : '▾';
  }
  apply();
  document.getElementById('queue-toggle').addEventListener('click', () => {
    collapsed = !collapsed;
    apply();
    try { localStorage.setItem('queueCollapsed', collapsed ? '1' : '0'); } catch (e) {}
  });
})();

// Create/switch workspace (direct user request - "allow user to create a
// new workspace, to start from scratch," then a follow-up: "don't ask
// user to put the folder path manually - scan it and add options as
// clickable... everything should be clickable buttons"). No text input
// anywhere in this flow: switching means clicking a scanned sibling
// project's button, creating means clicking "+ New workspace" with no
// fields at all (the server auto-names it).
//
// A full page reload after a successful switch is deliberate, not a
// shortcut: the server now serves a different repo entirely, so the
// event-offset counter, history, and every "which repo am I looking at"
// assumption need to start fresh - a reload gets all of that correct for
// free instead of hand-rewriting client state to match a repo the page
// never loaded for.
async function goToWorkspace(path, msgEl) {
  msgEl.textContent = 'Working...'; msgEl.className = '';
  try {
    const res = await fetch('/api/workspace', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({path}),
    });
    const data = await res.json();
    if (!res.ok) { msgEl.textContent = 'Error: ' + (data.error || res.status); msgEl.className = 'err'; return; }
    msgEl.textContent = 'Switched to ' + data.repo + ' - reloading...'; msgEl.className = 'ok';
    setTimeout(() => window.location.reload(), 600);
  } catch (e) {
    msgEl.textContent = 'Request failed: ' + e; msgEl.className = 'err';
  }
}

async function loadWorkspaceOptions() {
  const container = document.getElementById('workspace-options');
  container.innerHTML = '<span id="workspace-loading">loading...</span>';
  try {
    const res = await fetch('/api/workspaces');
    const data = await res.json();
    if (!data.options || data.options.length === 0) {
      container.innerHTML = '<span id="workspace-empty">no other project found in ' + data.base_dir + '</span>';
      return;
    }
    container.innerHTML = '';
    data.options.forEach(opt => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'workspace-chip';
      btn.textContent = opt.name;
      btn.title = opt.path;
      btn.addEventListener('click', () => goToWorkspace(opt.path, document.getElementById('workspace-msg')));
      container.appendChild(btn);
    });
  } catch (e) {
    container.innerHTML = '<span id="workspace-empty">could not scan for projects</span>';
  }
}

document.getElementById('workspace-toggle').addEventListener('click', () => {
  const bar = document.getElementById('workspace-bar');
  const opening = bar.style.display === 'none';
  bar.style.display = opening ? 'flex' : 'none';
  if (opening) loadWorkspaceOptions();
});

document.getElementById('workspace-new-btn').addEventListener('click', async () => {
  const msg = document.getElementById('workspace-msg');
  msg.textContent = 'Creating...'; msg.className = '';
  try {
    const res = await fetch('/api/workspace/new', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}',
    });
    const data = await res.json();
    if (!res.ok) { msg.textContent = 'Error: ' + (data.error || res.status); msg.className = 'err'; return; }
    msg.textContent = 'Created ' + data.repo + ' - reloading...'; msg.className = 'ok';
    setTimeout(() => window.location.reload(), 600);
  } catch (e) {
    msg.textContent = 'Request failed: ' + e; msg.className = 'err';
  }
});

pollEvents();
pollHistory();
pollQueue();
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
                    .replace("__REPO_PATH__", html.escape(str(self.repo)))
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

        if parsed.path == "/api/workspaces":
            self._json({
                "base_dir": str(self.repo.parent),
                "current": str(self.repo),
                "options": self._scan_sibling_workspaces(),
            })
            return

        if parsed.path == "/api/queue":
            status = orchestrator.read_daemon_status(self.repo)
            daemon = {**status, "alive": orchestrator.daemon_is_alive(status)} if status else {"alive": False}
            self._json({
                "entries": list(reversed(orchestrator.all_queue_entries(self.repo))),
                "daemon": daemon,
            })
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
        if parsed.path == "/api/workspace":
            self._handle_workspace(body)
            return
        if parsed.path == "/api/workspace/new":
            self._handle_workspace_new(body)
            return
        if parsed.path == "/api/queue":
            self._handle_queue_add(body)
            return
        if parsed.path == "/api/daemon/start":
            self._handle_daemon_start(body)
            return
        if parsed.path == "/api/daemon/stop":
            self._handle_daemon_stop(body)
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

        if orchestrator.daemon_is_alive(orchestrator.read_daemon_status(self.repo)):
            self._json({"error": "the 24/7 daemon is running against this repo - add this "
                                  "as a queue item in the queue panel instead of submitting "
                                  "directly, or stop the daemon first (touch .agent-queue/STOP)"},
                        status=409)
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

        if orchestrator.daemon_is_alive(orchestrator.read_daemon_status(self.repo)):
            self._json({"error": "the 24/7 daemon is running against this repo - wait "
                                  "for it to be idle or stop it first (touch "
                                  ".agent-queue/STOP) before pushing from here"}, status=409)
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

    MAX_WORKSPACE_OPTIONS = 12

    def _scan_sibling_workspaces(self) -> list[dict]:
        """Direct user request: "don't ask users to put the folder path
        manually. You scan it and add options as clickable." Scans the
        CURRENT repo's own parent directory (wherever it happens to live -
        for this project, ~/workspace/) for sibling directories that are
        themselves git repos, excluding the currently-active one. This is
        a best-effort convenience scan, not a filesystem browser - it only
        looks one directory up and one level deep, on purpose: a real file
        picker is a much bigger feature than "click a sibling project
        instead of typing its path".

        Sorted by most-recently-active first and capped at
        MAX_WORKSPACE_OPTIONS - found live (2026-08-27): this user's real
        ~/workspace/ has 57 sibling git repos. A flat alphabetical list
        that long defeats "a few clicks", and a text filter isn't an
        option given the explicit "no typing" requirement - recency is the
        one sort that needs no user input at all and matches what "which
        project was I just in" actually means. Uses .git/HEAD's mtime
        (updates on every commit/checkout) as a fast recency proxy - avoids
        running `git log` once per repo, which would mean 57 subprocess
        spawns just to open this panel."""
        base = self.repo.parent
        options = []
        try:
            for entry in sorted(base.iterdir()):
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                resolved = entry.resolve()
                if resolved == self.repo.resolve():
                    continue
                git_head = entry / ".git" / "HEAD"
                if not git_head.exists():
                    continue
                try:
                    mtime = git_head.stat().st_mtime
                except OSError:
                    mtime = 0
                options.append({"name": entry.name, "path": str(resolved), "_mtime": mtime})
            options.sort(key=lambda o: o["_mtime"], reverse=True)
            options = options[: self.MAX_WORKSPACE_OPTIONS]
            for o in options:
                del o["_mtime"]
        except OSError:
            pass  # base dir unreadable for some reason - just show no options, not an error
        return options

    def _handle_workspace(self, body: dict) -> None:
        """Switches to an EXISTING repo. The path comes from a button built
        from _scan_sibling_workspaces()'s server-provided list, not user
        typing - see the module note above. Synchronous (not a background
        thread, unlike submit/push): this is fast, and the frontend needs a
        definite answer before it reloads the page for the new repo.
        Refuses while a run is in progress against the CURRENT workspace,
        same RUN_LOCK guard as submit/push."""
        path_str = (body.get("path") or "").strip()
        if not path_str:
            self._json({"error": "path is required"}, status=400)
            return
        new_repo = Path(path_str).expanduser()
        if not new_repo.is_absolute():
            self._json({"error": "path must be absolute"}, status=400)
            return

        if not RUN_LOCK.acquire(blocking=False):
            self._json({"error": "a run is in progress against the current workspace - "
                                  "wait for it to finish before switching"}, status=409)
            return
        try:
            if not new_repo.is_dir():
                self._json({"error": f"{new_repo} does not exist"}, status=400)
                return
            if not (new_repo / ".git").exists():
                self._json({"error": f"{new_repo} is not a git repository"}, status=400)
                return
            Handler.repo = new_repo.resolve()
            self._json({"status": "ok", "repo": str(Handler.repo)})
        except Exception as e:  # noqa: BLE001 - surface the real filesystem error, don't swallow it
            self._json({"error": str(e)}, status=500)
        finally:
            RUN_LOCK.release()

    def _handle_workspace_new(self, body: dict) -> None:
        """Creates a brand new workspace with NO typed input at all - direct
        user request: "add an option to create a new folder... do not ask
        users to type something manually." Auto-names it with a timestamp
        (collision-proof without needing to scan for the next free number)
        inside the same parent directory _scan_sibling_workspaces() looks
        at, so it shows up as a sibling next time. mkdir + git init + one
        real commit (README.md), same as before - a fresh repo needs at
        least one commit for the rest of this project's branch-based
        workflow (checkout_task_branch etc.) to have something to branch
        from."""
        if not RUN_LOCK.acquire(blocking=False):
            self._json({"error": "a run is in progress against the current workspace - "
                                  "wait for it to finish before creating a new one"}, status=409)
            return
        try:
            base = self.repo.parent
            name = "workspace-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            new_repo = base / name
            new_repo.mkdir(parents=True, exist_ok=False)
            orchestrator.run(["git", "init", "-q"], cwd=new_repo, check=True)
            (new_repo / "README.md").write_text(f"# {name}\n")
            orchestrator.run(["git", "add", "-A"], cwd=new_repo, check=True)
            orchestrator.run(["git", "commit", "-q", "-m", "Initial commit"],
                              cwd=new_repo, check=True)
            Handler.repo = new_repo.resolve()
            self._json({"status": "ok", "repo": str(Handler.repo)})
        except Exception as e:  # noqa: BLE001 - surface the real git/filesystem error, don't swallow it
            self._json({"error": str(e)}, status=500)
        finally:
            RUN_LOCK.release()

    def _handle_queue_add(self, body: dict) -> None:
        """Appends tasks to .agent-queue for the 24/7 daemon (chief.py
        --daemon, run separately via daemon-session.sh) to pick up - this
        dashboard process never runs them itself. One task per line in a
        textarea (also accepts a JSON list, for anything scripting this
        endpoint directly). No RUN_LOCK needed: enqueue_tasks only writes
        JSON files to .agent-queue, it never touches git state, so it can't
        conflict with a run in progress in this process OR the daemon's."""
        raw = body.get("tasks")
        if isinstance(raw, str):
            tasks = [t.strip() for t in raw.splitlines() if t.strip()]
        elif isinstance(raw, list):
            tasks = [str(t).strip() for t in raw if str(t).strip()]
        else:
            tasks = []
        if not tasks:
            self._json({"error": "tasks is required: a newline-separated string "
                                  "or a list of strings"}, status=400)
            return
        paths = orchestrator.enqueue_tasks(self.repo, tasks)
        self._json({"status": "queued", "count": len(paths)})

    def _daemon_session_name(self) -> str:
        return f"daemon-{self.repo.name}"

    def _handle_daemon_start(self, body: dict) -> None:
        """Spawns the daemon as its own tmux session - same NAME convention
        as daemon-session.sh ("daemon-<basename>") so attaching to it from
        a terminal (`tmux attach -t daemon-<repo>`) or running
        daemon-session.sh again both find the exact session this started,
        rather than creating a second, colliding one. Runs as a real OS
        process independent of this dashboard - stopping the dashboard (or
        it crashing) does NOT stop the daemon, same as if you'd started it
        from a terminal yourself."""
        status = orchestrator.read_daemon_status(self.repo)
        if orchestrator.daemon_is_alive(status):
            self._json({"error": "daemon is already running"}, status=409)
            return
        model = body.get("model") or orchestrator.MODEL_QUEUED
        if model not in set(orchestrator.AVAILABLE_MODELS):
            self._json({"error": f"model must be one of {sorted(orchestrator.AVAILABLE_MODELS)}"},
                        status=400)
            return

        name = self._daemon_session_name()
        chief_path = Path(__file__).resolve().parent / "chief.py"
        # Clears out a stale same-named session (e.g. left behind by an
        # unclean shutdown) so `tmux new-session` below doesn't fail with
        # "duplicate session" - harmless no-op if no such session exists.
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
        cmd_str = (f"python3 {shlex.quote(str(chief_path))} --daemon "
                   f"{shlex.quote(str(self.repo))} --model {shlex.quote(model)}")
        try:
            subprocess.run(
                ["tmux", "new-session", "-d", "-s", name, "-c", str(self.repo), cmd_str],
                check=True, capture_output=True, text=True,
            )
        except FileNotFoundError:
            self._json({"error": "tmux is not installed or not on PATH"}, status=500)
            return
        except subprocess.CalledProcessError as e:
            self._json({"error": f"failed to start tmux session: {e.stderr.strip()}"}, status=500)
            return
        self._json({"status": "started", "session": name, "model": model})

    def _handle_daemon_stop(self, body: dict) -> None:
        """Default is graceful: sets the STOP flag file the daemon already
        polls for between queue items (see process_council_queue) - it
        finishes whatever task is currently running first, same as
        touching .agent-queue/STOP by hand. force=True kills the tmux
        session outright instead, abandoning whatever the current item was
        mid-doing - that's exactly the D6 crash-resume scenario
        (checkout_task_branch discards uncommitted state, the queue entry
        stays "running" and gets picked back up as the next item on the
        daemon's next start), not a new risk this button introduces."""
        status = orchestrator.read_daemon_status(self.repo)
        if not orchestrator.daemon_is_alive(status):
            self._json({"error": "daemon is not running"}, status=409)
            return
        if body.get("force"):
            subprocess.run(["tmux", "kill-session", "-t", self._daemon_session_name()],
                            capture_output=True)
            self._json({"status": "killed"})
            return
        orchestrator.request_daemon_stop(self.repo)
        self._json({"status": "stop_requested"})


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
