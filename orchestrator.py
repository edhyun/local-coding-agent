#!/usr/bin/env python3
"""
Orchestrator wrapper for the local always-on coding agent (D7).

Design doc: ~/.gstack/projects/workspace/dahyun-unknown-design-20260823-121559.md

Implements, in one script:
  D2 - failure handling: single qwen3-coder:30b attempt, FAILED on failure,
       no escalation tier (routing dropped per T7).
  D4 - refuse to start a task on a dirty working tree.
  D6 - crash-resume: discard any uncommitted state left on the task's own
       agent/<slug> branch before re-invoking, since D4 would otherwise
       block its own recovery path.
  D7 - invoke OpenCode fresh per attempt; verify success via `git diff`,
       never via OpenCode's own exit code (confirmed unreliable in the
       T1 spike - exit 0 on both success and two distinct failure modes).
  D8 - never use `--auto`; write a project-local opencode.jsonc with
       explicit permission rules (edit/bash: allow, external_directory:
       deny) so OpenCode's own permission system enforces the sandbox.

Bug found and fixed (2026-08-23, live against ~/workspace/council): the
first version of this script committed its own bootstrap files (a
.gitignore edit) to whatever branch happened to be checked out when the
script ran - polluting the user's real work branch, not a throwaway one.
Root cause: any repo-local file the wrapper writes shows up in `git status`
on ANY branch, since untracked files aren't branch-scoped. Fix: never touch
the tracked .gitignore at all. Register the wrapper's own files in
`.git/info/exclude` instead - a git mechanism built for exactly this
(per-clone-local ignore rules, never committed, invisible to any diff, and
independent of whatever branch is checked out). No bootstrap commit is
needed at all with this fix.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Mode-based model selection (2026-08-24, head-to-head comparison against
# ~/workspace/council-adjacent scratch tests): qwen3-coder:30b is faster but
# less reliable at actually completing tasks (hallucinated tool-call syntax,
# stops after reading/summarizing instead of writing); qwen3.8-27b-mlx is
# meaningfully more reliable but noticeably slower (66s-4min+ per task).
# T7 already established that "saves compute" isn't a real local benefit -
# only latency is, and only interactive use cares about latency. Detached
# queue: batches don't care how long a task takes, only whether it's
# correct - so they default to the more reliable, slower model.
MODEL_INTERACTIVE = "ollama/qwen3-coder:30b"
MODEL_QUEUED = "lmstudio/qwen3.8-27b-mlx"
# Added 2026-08-27 (user pulled this model locally): a third, larger local
# option via Ollama. Not yet given a MODEL_* role constant of its own (no
# established mode this project defaults to it for) - it's a selectable
# extra, not a new default for anything.
MODEL_QWEN32B = "ollama/qwen3:32b-q8_0"

# Every model the dashboard's dropdowns and _handle_submit's validation
# know about. Add a model here (and only here) to make it selectable
# everywhere at once, instead of updating multiple hardcoded option lists.
AVAILABLE_MODELS = [MODEL_INTERACTIVE, MODEL_QUEUED, MODEL_QWEN32B]

OPENCODE_PERMISSION_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "permission": {
        "edit": "allow",
        "bash": "allow",
        "read": "allow",  # found missing 2026-08-25: unlisted permissions
        # default to "ask", which silently rejects with no TTY to answer it
        # (or interrupts a real attended session with a prompt D8 was
        # supposed to make unnecessary) - read within the project dir is
        # safe, external_directory below is the actual boundary that matters
        "external_directory": "deny",
        # Added 2026-08-27 (real bug, see AGENT_INSTRUCTIONS_OVERRIDE's
        # comment below): a real "skill" tool invocation succeeded (real
        # tool_use event, status "completed") even with external_directory
        # already set to "deny" - meaning whatever mechanism backs OpenCode's
        # "skill" tool does not appear to be gated by that permission at
        # all. Verified live: adding this explicit deny stopped it across
        # 2 consecutive re-runs of the exact prompt that triggered it.
        "skill": "deny",
        # Found in the SAME live re-test: with skill denied, the model
        # instead made real outbound HTTP requests via a "webfetch" tool to
        # hallucinated URLs (techcrunch.com/forbes.com paths that don't
        # exist - both 404'd, but the requests genuinely left the machine).
        # webfetch was never gated by this config at all - this wrapper is
        # for local coding tasks against a local repo; unrestricted
        # internet access was never a requirement, so deny by default,
        # same reasoning as external_directory.
        "webfetch": "deny",
    },
}

# Added for the Chief of Staff layer (chief.py, 2026-08-26): a reviewer
# (QA/PM persona) reads and runs commands to inspect a diff but must never
# be able to edit files - it's a review, not a second implementation
# attempt. Same external_directory boundary as the engineer profile.
PERMISSION_PROFILES = {
    "engineer": OPENCODE_PERMISSION_CONFIG["permission"],
    "reviewer": {
        "edit": "deny",
        "bash": "allow",
        "read": "allow",
        "external_directory": "deny",
    },
}

AGENT_FILES = ["opencode.jsonc", "agent-run-log.jsonl", ".agent-queue/", ".agent-dashboard/", "AGENTS.md"]

# Real bug found live (2026-08-27): OpenCode walks UP the directory tree
# PAST the target repo's own git root looking for CLAUDE.md/AGENTS.md - run
# against ~/workspace/council, it found and read ~/workspace/CLAUDE.md
# (this wrapper's OWN gstack project config for the Claude Code session
# managing THIS project, completely unrelated to council's codebase). That
# file's "ALWAYS invoke [skill] as your FIRST action" routing rule made the
# model literally call a tool named "skill" with {"name": "plan-ceo-review"}
# for the task "what AI product will succeed?" - confirmed as a real
# structured tool_use event (not a hallucinated text block), followed by a
# response containing a verbatim Claude-Code-style "[NOTE] Some previous
# conversation history..." compaction message - strong evidence it was
# pattern-matching that skill's own instructions rather than doing the
# coding task at all.
# Kept deliberately generic (2026-08-27, revised after a live re-test): an
# earlier version of this text named "skill tool" and "gstack" explicitly,
# on the theory that spelling out what to avoid would stop it. Live
# evidence pointed the other way - the model kept independently trying to
# explore the exact external path this file warned about, on a later,
# unrelated question. Naming the specific thing not to do may itself be
# what draws attention to it. This version says only "stay inside this
# repository" and never names what's outside it - the real enforcement is
# the permission denials (skill/webfetch/external_directory: deny), not
# this file; treat this as a mild nudge, not the actual boundary.
AGENT_INSTRUCTIONS_OVERRIDE = (
    "# Agent Instructions (written by local-coding-agent's wrapper)\n\n"
    "You are a plain coding assistant. Work only with files inside this\n"
    "repository, using paths relative to it. Do not reference, search, or\n"
    "read anything outside this repository's own directory.\n"
)
QUEUE_DIR = ".agent-queue"

# Read-only monitoring dashboard (dashboard.py, 2026-08-26): every streamed
# OpenCode event already flows through invoke_opencode - this just also
# appends a role/run_id-tagged copy to a repo-local JSONL file, so a
# separate process (the dashboard's HTTP server) can tail it without any
# in-process pub/sub between orchestrator.py/chief.py and dashboard.py.
DASHBOARD_DIR = ".agent-dashboard"
DASHBOARD_EVENTS_FILE = "events.jsonl"


def emit_dashboard_event(repo: Path, entry: dict) -> None:
    d = repo / DASHBOARD_DIR
    d.mkdir(exist_ok=True)
    entry = {"ts": datetime.now(timezone.utc).isoformat(), **entry}
    with (d / DASHBOARD_EVENTS_FILE).open("a") as f:
        f.write(json.dumps(entry) + "\n")

# Found via a real end-to-end test (2026-08-24, agent-e2e-verify repo,
# PR #1): the model ran its own code to self-verify (a bash tool call), and
# commit_changes()'s `git add -A` blindly staged the resulting __pycache__
# bytecode file too - a nonsense binary landed in a real PR. Fresh repos
# without their own .gitignore have no protection against this; excluding
# common build-artifact junk generically, same mechanism as AGENT_FILES,
# covers it regardless of whether the target repo already gitignores these.
COMMON_JUNK = [
    "__pycache__/", "*.pyc", ".pytest_cache/", ".DS_Store",
    "node_modules/", "*.egg-info/", ".venv/",
]


class MissingExecutable(Exception):
    pass


def run(cmd, cwd, check=False):
    """Wraps subprocess.run so a missing executable (git, gh, opencode, the
    test runner - anything) produces a clear error instead of an unhandled
    FileNotFoundError traceback. Found via testing push_and_open_pr on a
    machine without `gh` installed (2026-08-23)."""
    try:
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, check=check
        )
    except FileNotFoundError:
        raise MissingExecutable(
            f"'{cmd[0]}' is not installed or not on PATH (tried to run: {' '.join(cmd)})"
        )


def slugify(task: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", task.lower()).strip("-")
    return slug[:40] or "task"


def task_hash(task: str) -> str:
    """Short, deterministic suffix so two different tasks that happen to
    truncate to the same 40-char slug never collide on branch name. Found
    live (2026-08-27): "Add a one-line docstring to the top of
    council/config.py..." and the same phrasing for a different file both
    slugify identically in their first 40 chars, so the second task's
    `git checkout -b` hit the first task's already-existing branch and
    crashed with git exit 128. Hashes the FULL task text (not the truncated
    slug), so this fixes the actual root cause. The same task text
    re-submitted still hashes to the same suffix - existing
    resume-on-identical-retry behavior (D6) is unaffected."""
    return hashlib.sha256(task.encode()).hexdigest()[:6]


def git_status_short(repo: Path) -> str:
    return run(["git", "status", "--short"], cwd=repo).stdout


def is_dirty(repo: Path) -> bool:
    return bool(git_status_short(repo).strip())


def ensure_permission_config(repo: Path, profile: str = "engineer") -> None:
    """D8: project-local opencode.jsonc, never the global config. Always
    rewritten (found a real bug 2026-08-25: write-if-missing meant a repo
    that already had a stale config from earlier testing never picked up
    the "read": "allow" fix - the file is wrapper-owned and git-excluded
    (never committed), so there's nothing of the user's to preserve by
    leaving it alone).

    profile selects which PERMISSION_PROFILES entry to write - "engineer"
    (default, can edit) or "reviewer" (read/bash only, added for chief.py)."""
    config_path = repo / "opencode.jsonc"
    config = {
        "$schema": "https://opencode.ai/config.json",
        "permission": PERMISSION_PROFILES[profile],
    }
    config_path.write_text(json.dumps(config, indent=2) + "\n")


def ensure_git_exclude(repo: Path) -> None:
    """Register the wrapper's own files, AND common build-artifact junk
    (COMMON_JUNK - see its comment), in .git/info/exclude - never the
    tracked .gitignore. This is per-clone-local, never committed, and never
    shows up in `git status` or any diff/PR on any branch. Fixes the
    bootstrap-commit-on-the-wrong-branch bug (see module docstring) - and
    the same reasoning is why COMMON_JUNK goes here too, not into a real
    .gitignore commit: writing to the tracked .gitignore would re-introduce
    that exact bug class (a commit landing on whatever branch happens to be
    checked out)."""
    exclude_path = repo / ".git" / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text() if exclude_path.exists() else ""
    lines = existing.splitlines()
    changed = False
    for f in AGENT_FILES + COMMON_JUNK:
        if f not in lines:
            lines.append(f)
            changed = True
    if changed:
        exclude_path.write_text("\n".join(lines) + "\n")


def ensure_agent_instructions_override(repo: Path) -> None:
    """Only writes AGENTS.md if the repo has NEITHER CLAUDE.md NOR AGENTS.md
    already - unlike opencode.jsonc, these are legitimate project
    convention files that might carry the user's own real content, so this
    must never silently overwrite one. Known gap: if a target repo already
    has one of these files, this mitigation does not apply, and the
    parent-directory CLAUDE.md leak described above (see AGENT_FILES'
    comment) may still occur - not silently pretended to be fixed for that
    case."""
    if (repo / "CLAUDE.md").exists() or (repo / "AGENTS.md").exists():
        return
    (repo / "AGENTS.md").write_text(AGENT_INSTRUCTIONS_OVERRIDE)


def branch_exists(repo: Path, branch: str) -> bool:
    result = run(["git", "rev-parse", "--verify", branch], cwd=repo)
    return result.returncode == 0


def current_branch(repo: Path) -> str:
    return run(["git", "branch", "--show-current"], cwd=repo).stdout.strip()


def checkout_task_branch(repo: Path, branch: str, resuming: bool) -> None:
    """D6: on resume, discard any uncommitted state left on the agent's own
    branch before reusing it - this is the agent's own incomplete work
    (the wrapper never auto-commits, so a crash only ever leaves
    *uncommitted* changes, never orphan commits), never main or the user's
    branches, so D4's dirty-tree refusal does not apply here. No reference
    to a remote/origin branch - most repos this runs against won't have
    this branch pushed anywhere."""
    if resuming:
        run(["git", "checkout", "--", "."], cwd=repo)
        run(["git", "clean", "-fd"], cwd=repo)
    else:
        run(["git", "checkout", "-b", branch], cwd=repo, check=True)


def print_event_live(event: dict) -> None:
    """D1's actual point: this is what makes attaching to the tmux session
    show real work happening, not a silent pause. Found missing (2026-08-25)
    when the user asked how to watch it - the previous version captured all
    output silently via subprocess.run and only reported a summary after the
    whole task finished, which defeats the design's core premise."""
    t = event.get("type")
    part = event.get("part", {})
    if t == "text":
        # Found 2026-08-27 (real user session): a 200-char cap cut off the
        # model's actual answer mid-word for a Q&A-style task, making a
        # perfectly good response look like it just trailed into nothing -
        # confusing on its own, and worse right before a "FAILED" line for a
        # task that was never going to produce a git diff in the first place.
        # 2000 is generous enough that a real answer isn't chopped; still
        # capped so one pathological wall of text can't flood the terminal.
        text = part.get("text", "").strip()
        if text:
            print(f"  · {text[:2000]}")
    elif t == "tool_use":
        state = part.get("state", {})
        tool = part.get("tool", "?")
        status = state.get("status", "?")
        if tool == "write":
            fp = state.get("input", {}).get("filePath", "?")
            print(f"  → write {fp} [{status}]")
        elif tool == "bash":
            cmd = state.get("input", {}).get("command", "")[:100]
            print(f"  → bash: {cmd} [{status}]")
        else:
            print(f"  → {tool} [{status}]")
        if status == "error":
            print(f"    (denied: {str(state.get('error',''))[:150]})")


def invoke_opencode(repo: Path, task: str, model: str, role: str = "Engineer",
                     run_id: str | None = None, round_num: int = 1) -> dict:
    """D7: fresh process per attempt, --format json for a parseable event
    stream, no --auto (D8 relies on the project-local permission config
    instead). Streams events live via print_event_live as they arrive
    (Popen + line-by-line read), not just parsed after the fact - this is
    what makes attaching to the tmux session actually show live progress.

    Real bug found and fixed (2026-08-25): subprocess.Popen(cwd=repo)
    changes the OS-level working directory of the child process, but by
    default inherits the *caller's* PWD environment variable unchanged.
    OpenCode's project-root detection appears to follow PWD rather than the
    actual process cwd - reproduced directly: launching this same call from
    two different shell directories (~/workspace/local-coding-agent, then
    /tmp) caused the model to go explore *that* directory instead of the
    target repo, every time, regardless of the correct `cwd=repo` already
    being passed. This was silently derailing real tasks and had likely
    been misread as pure model unreliability in earlier testing. Fix:
    explicitly set PWD (and OLDPWD, to avoid a stale value there confusing
    anything that reads it) in the child's environment to match repo."""
    env = os.environ.copy()
    env["PWD"] = str(repo)
    env.pop("OLDPWD", None)
    if run_id:
        emit_dashboard_event(repo, {
            "run_id": run_id, "role": role, "round": round_num,
            "kind": "start", "task": task, "model": model,
        })
    try:
        proc = subprocess.Popen(
            ["opencode", "run", task, "-m", model, "--format", "json"],
            cwd=repo,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        raise MissingExecutable(
            "'opencode' is not installed or not on PATH "
            "(brew install sst/tap/opencode)"
        )
    events = []
    for line in proc.stdout:
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.append(event)
        print_event_live(event)
        if run_id:
            emit_dashboard_event(repo, {
                "run_id": run_id, "role": role, "round": round_num,
                "kind": "opencode_event", "event": event,
            })
    stderr_tail = proc.stderr.read()[-500:]
    proc.wait()
    return {
        "returncode": proc.returncode,  # logged, never trusted (D7 finding)
        "events": events,
        "stderr_tail": stderr_tail,
    }


def detect_test_command(repo: Path) -> list[str] | None:
    """Best-effort test framework detection. Returns None if nothing
    recognized (safety gate is then skipped, PR gets [NO TESTS] - see
    design doc's Test gate decision)."""
    if (repo / "uv.lock").exists() and (repo / "pyproject.toml").exists():
        return ["uv", "run", "pytest"]
    if (repo / "pyproject.toml").exists() or (repo / "pytest.ini").exists():
        return ["pytest"]
    if (repo / "package.json").exists():
        try:
            pkg = json.loads((repo / "package.json").read_text())
            if "test" in pkg.get("scripts", {}):
                return ["npm", "test"]
        except (json.JSONDecodeError, OSError):
            pass
    if (repo / "Cargo.toml").exists():
        return ["cargo", "test"]
    if (repo / "go.mod").exists():
        return ["go", "test", "./..."]
    return None


def run_test_gate(repo: Path) -> dict:
    """Safety gate: run the target repo's test suite before a PR can open.

    Critical fix (2026-08-23, real incident against ~/workspace/council): a
    generated test's broken mock silently wrote a real row into the user's
    actual production database because the code under test fell back to a
    default path under the real Path.home(). File-system sandboxing (D8)
    does nothing to stop this - it's a legitimate write to a path the code
    is configured to use, not an out-of-scope write. Fix: override HOME (and
    XDG_DATA_HOME/XDG_CONFIG_HOME) to a scratch directory for the test
    subprocess only. Cheap, general, and does not require knowing any
    specific repo's own environment-variable escape hatches (e.g. council's
    COUNCIL_DB) - it catches the whole class of "defaults to somewhere under
    the real home directory" bugs.
    """
    cmd = detect_test_command(repo)
    if cmd is None:
        return {"status": "NO_TESTS"}

    scratch_home = tempfile.mkdtemp(prefix="agent-test-home-")
    try:
        env = os.environ.copy()
        env["HOME"] = scratch_home
        env["XDG_DATA_HOME"] = str(Path(scratch_home) / ".local" / "share")
        env["XDG_CONFIG_HOME"] = str(Path(scratch_home) / ".config")
        env["XDG_CACHE_HOME"] = str(Path(scratch_home) / ".cache")
        proc = subprocess.run(
            cmd, cwd=repo, capture_output=True, text=True, env=env, timeout=300
        )
        return {
            "status": "PASS" if proc.returncode == 0 else "FAIL",
            "command": cmd,
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-2000:],
            "stderr_tail": proc.stderr[-2000:],
        }
    finally:
        shutil.rmtree(scratch_home, ignore_errors=True)


def commit_changes(repo: Path, task: str) -> str:
    """Local, reversible - safe to do automatically once the gate passes.
    Pushing and opening a PR are NOT automatic (see push_and_open_pr): those
    are real-world-affecting actions and require the explicit --push flag."""
    run(["git", "add", "-A"], cwd=repo)
    message = f"Agent: {task[:72]}"
    run(["git", "commit", "-q", "-m", message], cwd=repo, check=True)
    return run(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()


def check_github_token() -> str | None:
    """D3: env var only, never written to disk. `gh` CLI reads GH_TOKEN or
    GITHUB_TOKEN natively - no manual token handling needed beyond checking
    it's actually set, so a missing token fails with a clear message instead
    of a confusing gh auth error."""
    return os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")


def push_and_open_pr(repo: Path, branch: str, task: str, gate: dict) -> dict:
    """Real-world-affecting: pushes to the remote and opens a real PR. Only
    called when the wrapper is explicitly run with --push. Refuses outright
    if the safety gate failed - the whole point of the gate is that a
    failing task never reaches this step, automatically or otherwise."""
    if gate["status"] == "FAIL":
        return {"status": "BLOCKED", "reason": "safety gate failed"}

    token = check_github_token()
    if not token:
        return {
            "status": "ERROR",
            "reason": "GH_TOKEN or GITHUB_TOKEN not set in environment "
                      "(D3: read from env var only, never stored on disk)",
        }

    if not shutil.which("gh"):
        return {
            "status": "ERROR",
            "reason": "gh CLI is not installed - required to open the PR "
                      "(found missing on this machine during testing, "
                      "2026-08-23). Install: brew install gh",
        }

    try:
        push = run(["git", "push", "-u", "origin", branch], cwd=repo)
        if push.returncode != 0:
            return {"status": "ERROR", "reason": f"push failed: {push.stderr[-500:]}"}

        title = task[:72]
        if gate["status"] == "NO_TESTS":
            title = f"[NO TESTS - MANUAL REVIEW] {title}"

        pr = run(
            ["gh", "pr", "create", "--draft", "--title", title,
             "--body", f"Opened by the local coding agent.\n\nTask: {task}\n\n"
                        f"Safety gate: {gate['status']}"],
            cwd=repo,
        )
        if pr.returncode != 0:
            return {"status": "ERROR", "reason": f"gh pr create failed: {pr.stderr[-500:]}"}
        return {"status": "OPENED", "pr_output": pr.stdout.strip()}
    except MissingExecutable as e:
        return {"status": "ERROR", "reason": str(e)}


def queue_dir(repo: Path) -> Path:
    d = repo / QUEUE_DIR
    d.mkdir(exist_ok=True)
    return d


def enqueue_tasks(repo: Path, tasks: list[str]) -> list[Path]:
    """Maildir-style spool (Open Questions: 'file-based/maildir-style spool
    vs. SQLite - not yet decided; either is fine at this scale' - picked
    file-based, no new dependency). One JSON file per task, named so
    filename order == queue order. Written to disk before any task runs, so
    a crash mid-queue still has every remaining task recorded - this is
    what makes the queue survive a restart, not just the per-task D6
    resume semantics (which only cover one interrupted task's git state,
    not "what else was still queued")."""
    qdir = queue_dir(repo)
    existing = sorted(qdir.glob("*.json"))
    next_n = len(existing)
    paths = []
    for i, task in enumerate(tasks):
        p = qdir / f"{next_n + i:05d}.json"
        p.write_text(json.dumps({"task": task, "status": "pending"}))
        paths.append(p)
    return paths


def load_queue_entry(path: Path) -> dict:
    return json.loads(path.read_text())


def save_queue_entry(path: Path, entry: dict) -> None:
    path.write_text(json.dumps(entry))


def pending_queue_entries(repo: Path) -> list[Path]:
    """Includes 'running' entries too - if the process died mid-task, that
    entry was marked running and never got to done/failed. Picking it back
    up (not skipping it) is the actual crash-durability property; D6's
    checkout_task_branch already handles cleaning up whatever partial state
    that interrupted attempt left on disk."""
    qdir = queue_dir(repo)
    entries = []
    for p in sorted(qdir.glob("*.json")):
        entry = load_queue_entry(p)
        if entry["status"] in ("pending", "running"):
            entries.append(p)
    return entries


def process_queue(repo: Path, model: str) -> None:
    """Drains the queue, oldest first. Resuming after a restart is just
    calling this again - pending_queue_entries() finds exactly what's left,
    nothing needs to be told what to resume."""
    while True:
        pending = pending_queue_entries(repo)
        if not pending:
            print("\nQueue empty.")
            return
        path = pending[0]
        entry = load_queue_entry(path)
        task = entry["task"]
        print(f"\n--- queue: {task} ({len(pending) - 1} remaining after this) ---")
        entry["status"] = "running"
        save_queue_entry(path, entry)
        try:
            result = run_task(repo, task, model=model)
            entry["status"] = "done" if result["exit_code"] == 0 else "failed"
        except MissingExecutable as e:
            print(f"ERROR: {e}")
            entry["status"] = "failed"
            entry["error"] = str(e)
        save_queue_entry(path, entry)


def append_log(repo: Path, entry: dict) -> None:
    log_path = repo / "agent-run-log.jsonl"
    entry["timestamp"] = datetime.now(timezone.utc).isoformat()
    with log_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


# Real user feedback (2026-08-27, asked twice): "why does Engineer always
# handle my prompt even when it's not about coding" - e.g. "hello",
# "what AI product will succeed?". Added a routing check in front of the
# code-change pipeline, NOT inside it. This is deliberately different from
# the "no intent detection, ever" stance elsewhere in this project (D7):
# THAT stance is about never trusting a fuzzy guess for a SAFETY decision
# (whether a change is verified). THIS is a low-stakes routing decision -
# every existing safety property (git-diff verification, sandboxing, the
# test gate) is completely unchanged once inside run_task; this only
# decides whether to enter it at all. A wrong classification fails safe
# both ways: a real code task misclassified as a question just gets a
# plain answer with nothing changed (the user notices immediately and can
# resubmit - no false success); a real question misclassified as code
# falls through to run_task's existing NO_CHANGES path, identical to
# today's behavior before this existed.
PROVIDER_ENDPOINTS = {
    "ollama": "http://127.0.0.1:11434/v1/chat/completions",
    "lmstudio": "http://127.0.0.1:1234/v1/chat/completions",
}


def classify_intent(task: str, model: str) -> str:
    """Returns "CODE" or "QUESTION". A plain one-shot completion call
    directly against the model's own OpenAI-compatible endpoint - not
    OpenCode's agentic tool-use loop, since classification needs no tools
    and going through OpenCode would spend a whole process spin-up on a
    one-word answer. Any failure (unknown provider, network error,
    unparseable response) returns "CODE" - fails toward the existing,
    already-verified pipeline rather than a new, less-tested path."""
    provider, _, real_model = model.partition("/")
    endpoint = PROVIDER_ENDPOINTS.get(provider)
    if not endpoint:
        return "CODE"
    prompt = (
        "Reply with exactly one word, nothing else: CODE if the following "
        "task asks to write, modify, fix, or refactor code or files; "
        "QUESTION if it is asking for information, an opinion, an "
        "explanation, or general conversation with no code change "
        "intended.\n\nTask: " + task
    )
    try:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps({
                "model": real_model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 10,
                "temperature": 0,
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
        answer = data["choices"][0]["message"]["content"].strip().upper()
        return "QUESTION" if answer.startswith("QUESTION") else "CODE"
    except (urllib.error.URLError, OSError, KeyError, IndexError, json.JSONDecodeError):
        return "CODE"


def handle_goal(repo: Path, task: str, model: str, push: bool = False,
                 run_id: str | None = None, round_num: int = 1) -> dict:
    """Entry point for a FRESH user goal of unknown intent (repl(), main(),
    chief.run_council()'s initial goal) - classifies before deciding
    whether to enter the code-change pipeline at all. Internal callers that
    already know a task is code-related (chief.py's revision round,
    queue: batches someone explicitly typed as tasks) should keep calling
    run_task directly, skipping this classification - it would only add
    latency and a small chance of a wrong answer to something already
    known.

    On QUESTION: never creates a branch, never commits - there is nothing
    to verify via git diff because nothing should have changed under
    read-only ("reviewer") permissions. Success here means the model
    answered, which invoke_opencode's live event stream already showed;
    this just records that outcome instead of running it through the
    code-change verification machinery that doesn't apply.

    Found live (2026-08-27, real user report): the QUESTION path used to
    pass through the caller's `role` param unchanged, which defaults to
    "Engineer" - so a correctly-classified question still displayed as
    "Engineer" in the terminal/dashboard, making a real routing fix look
    like it hadn't done anything. Now always labeled "Assistant" here,
    distinct from the code-change roles, so a working classification is
    visibly different, not just internally different."""
    repo = repo.resolve()
    ensure_git_exclude(repo)
    ensure_agent_instructions_override(repo)
    intent = classify_intent(task, model)
    if intent == "QUESTION":
        run_id = run_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{slugify(task)}"
        print(f"Routed as a question, not a code change - answering directly "
              f"(no branch, no commit).")
        ensure_permission_config(repo, profile="reviewer")
        # Explicit "stay in this repo" framing added to the prompt itself
        # (2026-08-27, real bug): a plain question still led the model to
        # grep an out-of-repo path (/Users/dahyun/.agents/skills/gstack) -
        # correctly DENIED by external_directory, but the denial message
        # is a large dump of the full permission rule list, which then
        # derailed the model into "debugging" that dump instead of
        # answering. Reducing the chance it wanders there at all is cheaper
        # than trying to make a bad answer graceful after the fact.
        scoped_task = (
            f"{task}\n\n(Answer using only this repository's own files, at "
            f"relative paths under the current directory. Do not reference "
            f"or search any path outside this repository.)"
        )
        invoke_opencode(repo, scoped_task, model, role="Assistant", run_id=run_id, round_num=round_num)
        emit_dashboard_event(repo, {"run_id": run_id, "role": "Assistant", "round": round_num,
                                     "kind": "end", "status": "ANSWERED"})
        append_log(repo, {"task": task, "status": "ANSWERED",
                           "note": "classified as a question, answered directly, no branch/commit"})
        return {"exit_code": 0, "status": "ANSWERED", "branch": None,
                "base_branch": current_branch(repo)}
    return run_task(repo, task, push=push, model=model, run_id=run_id, round_num=round_num)


def run_task(repo: Path, task: str, push: bool = False, model: str = MODEL_INTERACTIVE,
             run_id: str | None = None, round_num: int = 1) -> dict:
    """Returns a result dict (not just an exit code, since 2026-08-26 - the
    Chief of Staff layer in chief.py needs the branch/base_branch/gate to
    hand the same commit to a reviewer persona afterward). Always has
    "exit_code" (same semantics as the old bare-int return: 0 success,
    1 failure/blocked, 2 refused-dirty) and "status"; "branch",
    "base_branch", "commit", "gate" are present once the task actually
    got as far as touching a branch."""
    repo = repo.resolve()
    slug = slugify(task)
    branch = f"agent/{slug}-{task_hash(task)}"
    base_branch = current_branch(repo)
    run_id = run_id or f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{slug}"

    def finish(result: dict) -> dict:
        emit_dashboard_event(repo, {
            "run_id": run_id, "role": "Engineer", "round": round_num,
            "kind": "end", "status": result["status"],
        })
        return result

    # Exclude registration happens on whatever branch we start on - it's
    # fully safe now, since .git/info/exclude is repo-wide and never
    # committed, so it can never make D4's check below fire, on this
    # branch or any other.
    ensure_git_exclude(repo)
    ensure_agent_instructions_override(repo)

    resuming = branch_exists(repo, branch) and current_branch(repo) == branch
    if not resuming and is_dirty(repo):
        # D4: refuse to start a *new* task on a dirty tree - checked on the
        # branch the user actually had checked out, before we ever touch it.
        print(f"REFUSED: working tree is dirty. Commit or stash first.\n{git_status_short(repo)}")
        append_log(repo, {"task": task, "branch": branch, "status": "REFUSED_DIRTY"})
        return finish({"exit_code": 2, "status": "REFUSED_DIRTY", "branch": branch, "base_branch": base_branch})

    checkout_task_branch(repo, branch, resuming)

    # The permission config only needs to exist once we're actually on the
    # agent's own branch, about to invoke OpenCode - and since it's
    # git-excluded, it never needs a commit at all.
    ensure_permission_config(repo)

    print(f"Running OpenCode ({model}) on branch {branch}...")
    result = invoke_opencode(repo, task, model, role="Engineer", run_id=run_id, round_num=round_num)

    diff = run(["git", "diff", "--stat"], cwd=repo).stdout.strip()
    status = git_status_short(repo).strip()
    changed = bool(diff or status)

    if not changed:
        # D2 / D7: no escalation tier exists (T7). Exit code is not trusted -
        # "no changes" is the actual failure signal, regardless of what
        # OpenCode's process returned or what the model's text claimed.
        #
        # Found 2026-08-27 (real user session): "FAILED" reads as an error
        # even when nothing actually went wrong - it just means this specific
        # task was never going to produce a git diff, most commonly because
        # the task was a question ("scan the repo and understand what it
        # is"), not a change request. This tool's entire value (D7's
        # diff-verification, the safety gate, the review pipeline) only
        # applies to CODE CHANGES - it has no routing or intent detection
        # (never will, by design: guessing intent from task text is exactly
        # the kind of untrusted narration this project refuses to rely on).
        # Found 2026-08-27 (real user session, second half of the same
        # confusion the message-only fix above didn't fully close): the
        # dashboard renders this status value directly ("Engineer finished:
        # " + status), so leaving the internal enum as "FAILED" meant the
        # word "FAILED" still showed up verbatim for a plain "hello" prompt
        # that never had anything fail - only the printed terminal message
        # was fixed before, not the value other consumers (dashboard, log)
        # actually display. Renamed at the source instead of patching each
        # display site separately.
        print("NO CODE CHANGES (verified via git diff, not exit code) - this "
              "doesn't necessarily mean something went wrong. The model's "
              "answer, if any, is in the text above. If your goal was a "
              "question rather than a change request, this wrapper isn't "
              "the right tool for it - run `opencode run \"<question>\"` "
              "directly instead.")
        append_log(repo, {
            "task": task, "branch": branch, "status": "NO_CHANGES",
            "opencode_returncode": result["returncode"],
            "note": "no git changes detected",
        })
        return finish({"exit_code": 1, "status": "NO_CHANGES", "branch": branch, "base_branch": base_branch})

    print(f"Changes detected:\n{diff}")

    print("Running safety gate (test suite, HOME-isolated)...")
    gate = run_test_gate(repo)

    if gate["status"] == "NO_TESTS":
        print("SAFETY GATE: no test framework detected - PR would be prefixed "
              "[NO TESTS - MANUAL REVIEW], not silently unguarded.")
    elif gate["status"] == "PASS":
        print(f"SAFETY GATE: PASS ({' '.join(gate['command'])})")
    else:
        print(f"SAFETY GATE: FAIL ({' '.join(gate['command'])}, "
              f"exit {gate['returncode']}) - PR would be BLOCKED, not opened.")
        print(gate["stdout_tail"][-1000:])

    log_entry = {
        "task": task, "branch": branch, "status": "CHANGES_PRODUCED",
        "opencode_returncode": result["returncode"],
        "diff_stat": diff,
        "gate_status": gate["status"],
    }

    if gate["status"] == "FAIL":
        # Hard stop: no commit, no push, no PR, regardless of --push.
        print("BLOCKED: safety gate failed. Not committing, not opening a PR. "
              "Branch left as-is for manual inspection.")
        log_entry["status"] = "BLOCKED_GATE_FAIL"
        append_log(repo, log_entry)
        return finish({"exit_code": 1, "status": "BLOCKED_GATE_FAIL", "branch": branch,
                "base_branch": base_branch, "gate": gate})

    commit_sha = commit_changes(repo, task)
    print(f"Committed {commit_sha[:8]} on {branch}.")
    log_entry["commit"] = commit_sha

    if not push:
        print(f"Branch {branch} committed, ready for review.")
        print("Next: re-run with --push to push and open a draft PR "
              "(requires GH_TOKEN or GITHUB_TOKEN in the environment, D3).")
        log_entry["status"] = "COMMITTED"
        append_log(repo, log_entry)
        return finish({"exit_code": 0, "status": "COMMITTED", "branch": branch,
                 "base_branch": base_branch, "commit": commit_sha, "gate": gate})

    print("Pushing and opening a draft PR (--push)...")
    pr_result = push_and_open_pr(repo, branch, task, gate)
    log_entry["pr_result"] = pr_result

    if pr_result["status"] == "OPENED":
        print(f"PR opened (draft): {pr_result['pr_output']}")
        log_entry["status"] = "PR_OPENED"
        append_log(repo, log_entry)
        return finish({"exit_code": 0, "status": "PR_OPENED", "branch": branch,
                 "base_branch": base_branch, "commit": commit_sha, "gate": gate,
                 "pr_result": pr_result})
    else:
        print(f"PR NOT opened: {pr_result['status']} - {pr_result.get('reason')}")
        log_entry["status"] = "PR_FAILED"
        append_log(repo, log_entry)
        return finish({"exit_code": 1, "status": "PR_FAILED", "branch": branch,
                 "base_branch": base_branch, "commit": commit_sha, "gate": gate,
                 "pr_result": pr_result})


def repl(repo: Path) -> None:
    """Interactive loop (D1): the process this runs as is what tmux keeps
    alive across attach/detach. Attaching means watching this prompt live;
    detaching leaves it exactly where it was - Ctrl-b d, not a kill. A
    `queue: task one ; task two ; task three` line runs tasks back to back
    without further input, so you can hand it a batch and detach - the
    "keep working while I'm away, only when I say so" property from the
    design doc's Problem Statement."""
    repo = repo.resolve()
    ensure_git_exclude(repo)
    ensure_agent_instructions_override(repo)
    print(f"Local coding agent - {repo}")
    print(f"Interactive tasks use {MODEL_INTERACTIVE} (fast). "
          f"queue: batches use {MODEL_QUEUED} (slower, more reliable - "
          f"you're not watching in real time, so correctness matters more "
          f"than speed there).")

    pending = pending_queue_entries(repo)
    if pending:
        print(f"\nFound {len(pending)} unfinished queue item(s) from a previous "
              f"run (crash-resume) - continuing them before taking new input.")
        process_queue(repo, model=MODEL_QUEUED)

    print("This is for CODE CHANGES only - success is verified by git diff, "
          "not by the model's answer. For a plain question, run "
          "`opencode run \"<question>\"` directly instead; routed through "
          "here it'll report no changes, which is correct but easy to "
          "misread as an error.")
    print("Enter a task (add --push to open a PR on success), 'queue: t1 ; t2 ; ...' "
          "for a batch, or 'exit'.")
    while True:
        try:
            line = input("\ntask> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            return
        if not line or line.lower() in ("exit", "quit"):
            return
        if line.lower().startswith("queue:"):
            tasks = [t.strip() for t in line[len("queue:"):].split(";") if t.strip()]
            enqueue_tasks(repo, tasks)
            print(f"Wrote {len(tasks)} task(s) to {QUEUE_DIR}/ - persists across "
                  f"a crash/restart. Running sequentially with {MODEL_QUEUED}...")
            process_queue(repo, model=MODEL_QUEUED)
            continue
        push = False
        if line.endswith("--push"):
            push = True
            line = line[: -len("--push")].strip()
        try:
            handle_goal(repo, line, model=MODEL_INTERACTIVE, push=push)
        except MissingExecutable as e:
            print(f"ERROR: {e}")


def main():
    args = sys.argv[1:]
    if "--repl" in args:
        args.remove("--repl")
        if len(args) != 1:
            print("usage: orchestrator.py --repl <repo-path>")
            sys.exit(2)
        repl(Path(args[0]))
        return

    push = "--push" in args
    if push:
        args.remove("--push")
    if len(args) < 2:
        print("usage: orchestrator.py <repo-path> <task description> [--push]")
        print("       orchestrator.py --repl <repo-path>")
        sys.exit(2)
    repo = Path(args[0])
    task = " ".join(args[1:])
    try:
        sys.exit(handle_goal(repo, task, model=MODEL_INTERACTIVE, push=push)["exit_code"])
    except MissingExecutable as e:
        print(f"ERROR: {e}")
        sys.exit(3)


if __name__ == "__main__":
    main()
