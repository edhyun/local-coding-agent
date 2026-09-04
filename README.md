# local-coding-agent

A small wrapper around [OpenCode](https://github.com/sst/opencode) that runs
coding tasks against local models (Ollama / LM Studio) instead of a cloud
API, adds a lightweight 3-role review pass (Engineer → QA → PM), and gives
you a live, watchable, interactive session via tmux instead of a silent
background job.

## What this actually is (read before you get excited)

This was built to solve one specific personal problem: hitting API token
quota limits and wanting a coding agent that could run for a long time
without that ceiling. It is **not** presented as better than
[Aider](https://github.com/paul-gauthier/aider),
[Cline](https://github.com/cline/cline),
[OpenHands](https://github.com/All-Hands-AI/OpenHands), or
[CrewAI](https://github.com/joaomdmoura/crewAI) — in fact, CrewAI's
`role`/`goal`/`backstory` agent model is architecturally very close to the
Engineer/QA/PM pattern here, at a scale (60% of the Fortune 500) this
project has no claim to compete with. This is a personal tool, published to
see whether anyone besides its author finds it useful. If that's you, open
an issue and say so — that's the actual experiment.

## What it does

- **`orchestrator.py`** — runs one task against a local model via OpenCode:
  checks out a dedicated branch, verifies success by `git diff` (never by
  trusting the model's own exit code or narration), runs your test suite in
  a HOME-isolated environment before committing, and only pushes/opens a
  draft PR if you explicitly ask (`--push`).
- **`chief.py`** — the same executor, run three times with different
  role-prompts: an Engineer implements the task, then a QA persona and a PM
  persona each review the diff (read-only, can't edit) and have to end with
  a `VERDICT: APPROVE` or `VERDICT: REQUEST_CHANGES: <reason>`. One bounded
  revision round if either objects, then it escalates to you rather than
  looping.
- **`dashboard.py`** — a local web page (`127.0.0.1` only, no auth) that
  shows the above running live (streamed model output, per-role status,
  run history, collapsible) and lets you submit a goal and approve a push
  from the page itself. Also lets you add extra reviewer roles from a set
  of presets (UI/UX, Security, Performance, Accessibility, Docs) with one
  click each, or a fully custom one, and switch to - or create from
  scratch (`git init` + an empty first commit) - a different project
  directory without restarting the server. No auth means anything that can
  reach this port can trigger real runs and pushes, not just read history -
  accepted here because the same no-auth-on-localhost boundary already
  covers the terminal REPLs below; this just gives that same trusted actor
  (you, on your machine) a second way in. Every push still requires an
  explicit click, same gate as the terminal's `--push` flag - nothing
  pushes automatically.
- **`chief.py --daemon`** (via `daemon-session.sh`) — a 24/7 worker: give
  it a to-do list (queue tasks from the dashboard's queue panel, or
  `queue: t1 ; t2 ; ...` in `chief-session.sh`'s REPL) and it drains them
  one at a time through the same Engineer/QA/PM pipeline, forever - it
  never exits on an empty queue, it polls and waits for more. Commits only
  and never auto-pushes; anything that can't get QA/PM approval after the
  one bounded revision round is flagged `needs_human` in the queue and it
  moves on to the next item rather than blocking on it. Stops only on an
  explicit signal (`touch .agent-queue/STOP` in the repo, or Ctrl-C the
  session) or if the environment itself is broken (e.g. OpenCode isn't
  runnable) - a broken environment would fail every remaining item
  identically, so it stops instead of hot-looping. The dashboard refuses
  to run a submit/push of its own while the daemon's heartbeat looks alive
  (a real cross-process check, not just a UI hint) - don't also run
  `agent-session.sh`/`chief-session.sh` against the same repo while the
  daemon is active, same caveat as running any two of these at once.

## Setup

You need all of the following actually installed and running — this is not
packaged or pinned to specific versions yet:

- [OpenCode](https://github.com/sst/opencode) (`brew install sst/tap/opencode`)
- [Ollama](https://ollama.com), with `qwen3-coder:30b` pulled — used for
  interactive tasks (`ollama pull qwen3-coder:30b`)
- **Optional, Apple Silicon only:** [LM Studio](https://lmstudio.ai), with a
  model loaded and its local server running — used for `queue:` batches,
  where reliability matters more than speed. The example config below uses
  an MLX model (`qwen3.8-27b-mlx`), which only runs on Apple Silicon. On
  Linux/Windows, either substitute a non-MLX model in LM Studio, or skip
  `queue:` batches entirely and use interactive mode (Ollama) only — that
  path is cross-platform.
- Your global `~/.config/opencode/opencode.jsonc` needs `ollama` (and
  optionally `lmstudio`) registered as providers pointing at
  `http://127.0.0.1:11434/v1` and `http://127.0.0.1:1234/v1` respectively.

## Running it

```bash
# Interactive single-agent, watch it live in tmux
./agent-session.sh /path/to/some/repo

# The Engineer/QA/PM review pipeline
./chief-session.sh /path/to/some/repo

# A live monitor - and a second way to submit goals/approve pushes (separate terminal)
./dashboard-session.sh /path/to/some/repo

# The 24/7 worker - drains a to-do list through Engineer/QA/PM forever
./daemon-session.sh /path/to/some/repo
```

Each of these starts (or reattaches to) a tmux session named
`agent-<repo>` / `chief-<repo>` / `dashboard-<repo>` / `daemon-<repo>`.
Detach with `Ctrl-b d` — it keeps running; reattach later with the same
command. Don't run more than one of these against the same repo at the
same time (the dashboard's own submit/push refuse while the daemon looks
alive, but the two terminal REPLs don't know about each other or the
daemon) — same "one working tree, one active writer" rule as any two
people pushing to the same branch.

**Set your expectations on first run.** The reliable model path (LM
Studio) takes 66 seconds to 4+ minutes per model call. `chief.py` makes
three calls per task minimum (Engineer, then QA, then PM), sequentially —
your first council run on a trivial task can easily take 10+ minutes. This
is not the tool hanging.

## Running the tests

```bash
python3 -m unittest test_orchestrator.py test_chief.py -v
```

No dependencies beyond the Python standard library and a working `git` — no
`pip install` needed to run the test suite. These cover the pure logic
(slugify, permission profiles, the crash-resume queue, verdict parsing) and
git/gh-subprocess-dependent functions that don't require a live model.
They do **not** cover `invoke_opencode`, `run_task`, `run_council`, or the
dashboard's HTTP handler — those require a live local model and are
currently only verified manually. Mocking those out for real automated
coverage is open, tracked work (not included in this repo's tests yet).

## Known limitations

- No packaging, no version pinning, no CI. This is a demand test, not a
  1.0 release.
- Sequential only — Engineer, QA, and PM never run in parallel, by design
  (avoids contending for one machine's local inference).
- The dashboard has no authentication. It binds to `127.0.0.1` by default;
  don't bind it wider unless you understand that tradeoff.

## License

MIT — see `LICENSE`.
