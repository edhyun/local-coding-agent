#!/usr/bin/env python3
"""
Chief of Staff layer on top of orchestrator.py (2026-08-26).

Motivated by comparing our single-agent executor against the "Chief of
Staff" multi-agent pattern (a coordinator that decomposes a goal, delegates
to specialists, synthesizes their output, and gates anything external
behind an explicit human yes). orchestrator.py already IS a proven,
verified specialist executor (git-diff-verified success, sandboxed
permissions, HOME-isolated test gate, crash-durable) - this file adds a
coordinator on top of it rather than replacing anything.

v1 scope (deliberately small - "start with the Chief + a few high-ROI
specialists" and "over-orchestration risk: keep the Chief focused on
coordination" both argued against building the full 10-persona council in
one shot):
  - Exactly 3 roles: Engineer (implements), QA (reviews the diff for
    correctness), PM (judges the result against the original goal in
    plain, non-code terms). Originally all three used the SAME local model,
    differentiated only by role-prompt; per-role model override was added
    2026-08-27 (dashboard exposes it), but the roles themselves are still
    fixed at exactly these 3.
  - The "Chief" still does not decompose a goal across multiple
    specialists or pick which of several roles fits best - the pipeline is
    fixed (Engineer -> QA -> PM -> synthesis) once a goal is confirmed to
    be a code-change task. What was added 2026-08-27: a lightweight check
    in orchestrator.handle_goal() decides whether a goal is a code change
    at all before Engineer ever runs (see handle_goal's docstring) - this
    is routing INTO the pipeline, not routing WITHIN it across specialists;
    a real Chief-style decompose-and-route-across-specialists step still
    only earns its complexity once there's more than one specialist per
    kind of coding work to choose between.
  - Exactly one revision round if QA or PM request changes, then escalate
    to the human rather than looping - same "keep it simple, don't let
    coordination overhead multiply" reasoning that kept the original
    review-council design sequential instead of parallel.
  - No scheduling, no proactivity, no inter-agent messaging protocol - the
    human stays the only thing that kicks off a council run, same D1
    principle as orchestrator.py's repl.

Known v1 limitation, stated plainly: unlike the Engineer step (whose
success/failure is verified by `git diff`, never by the model's own
narration - see orchestrator.py's D7), QA's and PM's verdicts ARE the
model's self-report. There is no independent check that a reviewer
persona's "VERDICT: APPROVE" is actually correct. This is a real trust
gap, not an oversight - review judgment (as opposed to "did any file
change") isn't mechanically verifiable the way a diff is. Treat the
synthesis report as informative, not authoritative, until this has been
exercised enough to know how good the reviewer personas actually are.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

from orchestrator import (
    MODEL_INTERACTIVE,
    MODEL_QUEUED,
    MissingExecutable,
    append_log,
    current_branch,
    emit_dashboard_event,
    ensure_agent_instructions_override,
    ensure_git_exclude,
    ensure_permission_config,
    handle_goal,
    invoke_opencode,
    push_and_open_pr,
    run,
    run_task,
    slugify,
)

QA_PROMPT = """You are a skeptical senior QA engineer reviewing a colleague's finished change before it ships. You did not write this code and have no stake in defending it.

Original task the engineer was given: {task}

Run `git diff {base_branch}` (or `git log -p {base_branch}..HEAD`) to see exactly what changed on this branch, and open the affected files directly. Check for correctness bugs, missed edge cases, and whether the change actually does what the task asked. Do not edit any files - you are reviewing only, not fixing anything yourself.

End your reply with exactly one line starting with "VERDICT:" - either:
VERDICT: APPROVE
or
VERDICT: REQUEST_CHANGES: <specific, actionable reason>
"""

PM_PROMPT = """You are a pragmatic product manager with business acumen, judging a finished engineering change against what a stakeholder actually asked for - not code quality (QA already checked that), but whether it delivers what was asked, in terms a non-engineer would care about.

Original task: {task}

A QA engineer already reviewed this branch and said:
{qa_verdict_raw}

Run `git diff {base_branch}` to see what actually changed, then judge: does this genuinely satisfy the original ask? Do not edit any files - you are reviewing only.

End your reply with exactly one line starting with "VERDICT:" - either:
VERDICT: APPROVE
or
VERDICT: REQUEST_CHANGES: <specific reason, in plain terms>
"""


def build_custom_reviewer_prompt(role_name: str, instructions: str, task: str, base_branch: str) -> str:
    """User-defined reviewer roles (2026-08-27) - built directly with an
    f-string rather than a two-stage .format() template (fill in
    role_name/instructions, then task/base_branch) like QA_PROMPT/PM_PROMPT
    use. A second .format() pass over already-substituted user text would
    treat any literal "{" the user typed in their custom instructions as a
    new placeholder and raise KeyError. Filling in everything in one pass
    avoids that entirely."""
    return (
        f"You are a {role_name} reviewing a colleague's finished change before it ships. "
        f"You did not write this code and have no stake in defending it.\n\n"
        f"Original task: {task}\n\n"
        f"Your specific review focus: {instructions}\n\n"
        f"Run `git diff {base_branch}` to see exactly what changed on this branch, and "
        f"inspect the affected files directly. Do not edit any files - you are reviewing only.\n\n"
        f'End your reply with exactly one line starting with "VERDICT:" - either:\n'
        f"VERDICT: APPROVE\n"
        f"or\n"
        f"VERDICT: REQUEST_CHANGES: <specific, actionable reason>\n"
    )


def parse_verdict(events: list) -> dict:
    """Scans the model's text output for a line starting with "VERDICT:".
    Deliberately conservative: anything that isn't a clean APPROVE or
    REQUEST_CHANGES match comes back UNCLEAR, which the caller treats as
    "escalate to the human" rather than guessing which way it meant."""
    texts = [
        e.get("part", {}).get("text", "").strip()
        for e in events
        if e.get("type") == "text"
    ]
    full_text = "\n".join(t for t in texts if t)
    for line in reversed(full_text.splitlines()):
        line = line.strip()
        if line.upper().startswith("VERDICT:"):
            body = line.split(":", 1)[1].strip()
            if body.upper().startswith("APPROVE"):
                return {"verdict": "APPROVE", "raw": line}
            if body.upper().startswith("REQUEST_CHANGES"):
                reason = body.split(":", 1)[1].strip() if ":" in body else ""
                return {"verdict": "REQUEST_CHANGES", "reason": reason, "raw": line}
    return {"verdict": "UNCLEAR", "raw": full_text[-300:]}


def run_review(repo: Path, branch: str, role: str, prompt: str, model: str,
                run_id: str, round_num: int = 1) -> dict:
    """Checks out the engineer's branch read-only (reviewer permission
    profile: edit denied, read/bash allowed) and asks the role-prompted
    model for a verdict. Never commits, never modifies anything."""
    run(["git", "checkout", branch], cwd=repo, check=True)
    ensure_permission_config(repo, profile="reviewer")
    print(f"\n--- {role} reviewing branch {branch} ---")
    result = invoke_opencode(repo, prompt, model, role=role, run_id=run_id, round_num=round_num)
    verdict = parse_verdict(result["events"])
    print(f"{role} verdict: {verdict['verdict']}"
          + (f" - {verdict.get('reason', '')}" if verdict.get("reason") else ""))
    emit_dashboard_event(repo, {
        "run_id": run_id, "role": role, "round": round_num,
        "kind": "end", "status": verdict["verdict"],
    })
    return verdict


def run_council(repo: Path, goal: str, model: str = MODEL_INTERACTIVE, push: bool = False,
                 engineer_model: str | None = None, qa_model: str | None = None,
                 pm_model: str | None = None, extra_reviewers: list[dict] | None = None) -> dict:
    """The fixed Engineer -> QA -> PM -> synthesis pipeline, with one bounded
    revision round. Returns a report dict; never raises for a normal
    REQUEST_CHANGES/escalation outcome (only for MissingExecutable, which
    callers already handle the same way orchestrator.py's repl does).

    Per-role model override (2026-08-27, inspired by OpenExecutive's
    per-agent model dropdown): engineer_model/qa_model/pm_model each default
    to `model` when omitted, so every existing caller (repl(), main()) that
    only ever passed `model=` keeps its exact old behavior - uniform model
    across all three roles. Only the dashboard's submit form passes distinct
    values today.

    extra_reviewers (2026-08-27, direct user request: "I want to include a
    UI/UX designer for frontend work"): optional list of
    {"name": str, "instructions": str, "model": str} for user-defined
    reviewer roles beyond QA/PM. Deliberately NOT a full dynamic N-role
    roster system - CrewAI already does that at production scale (see the
    office-hours session that shaped this project's scope), and building
    a second one here would be exactly the kind of thing that design
    session decided not to chase. This is the minimum that answers the
    concrete ask: one or more extra reviewers, each independent (does not
    see other reviewers' verdicts, unlike PM seeing QA's), each running
    under the same read-only reviewer permission profile as QA/PM, each
    contributing to the same approve/escalate/revise decision."""
    engineer_model = engineer_model or model
    qa_model = qa_model or model
    pm_model = pm_model or model
    extra_reviewers = extra_reviewers or []
    repo = repo.resolve()
    base_branch = current_branch(repo)
    ensure_git_exclude(repo)
    ensure_agent_instructions_override(repo)
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{slugify(goal)}"

    # "Sending to Engineer" not "decomposing" - v1 still has no goal
    # decomposition/multi-specialist routing (see module docstring): every
    # coding goal that clears the intent check below goes to Engineer,
    # unconditionally, not to whichever of several specialists might fit
    # best. What changed 2026-08-27 (real user feedback, asked twice): a
    # lightweight low-stakes check now runs BEFORE this, in
    # handle_goal(), to decide whether to enter the code-change pipeline
    # at all - that's a routing decision (does this need Engineer touching
    # code), not a safety decision (D7's git-diff verification is
    # unchanged once inside the pipeline), so it doesn't conflict with
    # this project's refusal to guess intent for anything safety-critical.
    print(f"\n=== Chief: sending goal to Engineer ({engineer_model}) ===\n{goal}")
    eng_result = handle_goal(repo, goal, model=engineer_model, push=False,
                              run_id=run_id, round_num=1)

    if eng_result["status"] == "ANSWERED":
        print(f"\n=== Chief synthesis: ANSWERED - routed as a question, not a "
              f"code change. Nothing to review (QA/PM review diffs; there is "
              f"no diff). ===")
        report = {"status": "ANSWERED", "engineer": eng_result}
        append_log(repo, {"chief_report": report})
        return report

    if eng_result["exit_code"] != 0:
        print(f"\n=== Chief synthesis: NO CODE CHANGES at Engineer stage "
              f"({eng_result['status']}) - nothing to review. If this was a "
              f"question rather than a change request, the model's answer is "
              f"in the text above - the council is for code changes, not "
              f"general Q&A; try `opencode run \"<question>\"` directly for that. ===")
        # Same rename as orchestrator.py's NO_CHANGES (2026-08-27) - a
        # benign no-diff outcome (e.g. "hello") is not the same situation as
        # a real problem (dirty tree refused, or the safety gate failed),
        # so this outer status keeps that distinction rather than lumping
        # both under one alarming "FAILED" word.
        outer_status = ("ENGINEER_NO_CHANGES" if eng_result["status"] == "NO_CHANGES"
                         else "ENGINEER_BLOCKED")
        return {"status": outer_status, "engineer": eng_result}

    branch = eng_result["branch"]
    task_for_review = goal

    for attempt in (1, 2):
        qa_verdict = run_review(
            repo, branch, "QA",
            QA_PROMPT.format(task=task_for_review, base_branch=base_branch),
            qa_model, run_id=run_id, round_num=attempt,
        )
        pm_verdict = run_review(
            repo, branch, "PM",
            PM_PROMPT.format(task=task_for_review, base_branch=base_branch,
                              qa_verdict_raw=qa_verdict["raw"]),
            pm_model, run_id=run_id, round_num=attempt,
        )
        extra_verdicts = []
        for reviewer in extra_reviewers:
            prompt = build_custom_reviewer_prompt(
                reviewer["name"], reviewer["instructions"], task_for_review, base_branch)
            v = run_review(repo, branch, reviewer["name"], prompt,
                            reviewer.get("model") or model, run_id=run_id, round_num=attempt)
            extra_verdicts.append((reviewer["name"], v))

        # Return to the engineer's branch - reviewers may have left it
        # checked out mid-inspection.
        run(["git", "checkout", branch], cwd=repo)

        all_verdicts = [qa_verdict, pm_verdict] + [v for _, v in extra_verdicts]
        needs_changes = any(v["verdict"] != "APPROVE" for v in all_verdicts)
        escalate = any(v["verdict"] == "UNCLEAR" for v in all_verdicts)

        if not needs_changes:
            print(f"\n=== Chief synthesis: APPROVED after {attempt} round(s) "
                  f"(Engineer commit {eng_result.get('commit', '?')[:8]} on {branch}) ===")
            report = {
                "status": "APPROVED", "branch": branch, "rounds": attempt,
                "engineer": eng_result, "qa": qa_verdict, "pm": pm_verdict,
                "extra_reviewers": {name: v for name, v in extra_verdicts},
            }
            if push:
                gate = eng_result.get("gate", {"status": "NO_TESTS"})
                print("Human gate cleared (--push passed) - pushing and opening a draft PR...")
                pr_result = push_and_open_pr(repo, branch, goal, gate)
                report["pr_result"] = pr_result
                print(f"PR: {pr_result}")
            else:
                print(f"Branch {branch} approved by all reviewers, NOT pushed - "
                      f"re-run with --push for a draft PR (explicit human gate).")
            append_log(repo, {"chief_report": {k: v for k, v in report.items() if k != "pr_result"}})
            return report

        if escalate:
            print(f"\n=== Chief synthesis: ESCALATING to human - a reviewer's "
                  f"verdict was unclear rather than a clean APPROVE/REQUEST_CHANGES. ===")
            report = {
                "status": "ESCALATED", "branch": branch, "rounds": attempt,
                "engineer": eng_result, "qa": qa_verdict, "pm": pm_verdict,
                "extra_reviewers": {name: v for name, v in extra_verdicts},
            }
            append_log(repo, {"chief_report": report})
            return report

        if attempt == 2:
            print(f"\n=== Chief synthesis: NEEDS_HUMAN after {attempt} revision rounds - "
                  f"not looping further (over-orchestration risk). Branch {branch} left "
                  f"as-is for manual review. ===")
            report = {
                "status": "NEEDS_HUMAN", "branch": branch, "rounds": attempt,
                "engineer": eng_result, "qa": qa_verdict, "pm": pm_verdict,
                "extra_reviewers": {name: v for name, v in extra_verdicts},
            }
            append_log(repo, {"chief_report": report})
            return report

        # One bounded revision round: fold the reviewers' feedback into a
        # revised task and re-run the Engineer on the SAME branch. This is
        # exactly D6's crash-resume path (branch exists, already checked
        # out) doing double duty for revision, not a new mechanism.
        feedback = []
        if qa_verdict["verdict"] == "REQUEST_CHANGES":
            feedback.append(f"QA requested changes: {qa_verdict.get('reason', '')}")
        if pm_verdict["verdict"] == "REQUEST_CHANGES":
            feedback.append(f"PM requested changes: {pm_verdict.get('reason', '')}")
        for name, v in extra_verdicts:
            if v["verdict"] == "REQUEST_CHANGES":
                feedback.append(f"{name} requested changes: {v.get('reason', '')}")
        revision_task = (
            f"{goal}\n\nA reviewer asked for changes to your last attempt on this "
            f"branch:\n" + "\n".join(feedback)
        )
        print(f"\n=== Chief: one revision round - sending feedback back to Engineer ===")
        for line in feedback:
            print(f"  - {line}")
        run(["git", "checkout", branch], cwd=repo)
        eng_result = run_task(repo, revision_task, push=False, model=engineer_model,
                               run_id=run_id, round_num=attempt + 1)
        task_for_review = revision_task
        if eng_result["exit_code"] != 0:
            print(f"\n=== Chief synthesis: FAILED during revision "
                  f"({eng_result['status']}). ===")
            return {"status": "REVISION_FAILED", "branch": branch,
                     "engineer": eng_result, "qa": qa_verdict, "pm": pm_verdict}


def repl(repo: Path) -> None:
    repo = repo.resolve()
    ensure_git_exclude(repo)
    ensure_agent_instructions_override(repo)
    print(f"Chief of Staff - {repo}")
    print(f"Roster: Engineer, QA, PM (all {MODEL_INTERACTIVE}, role-prompted). "
          f"One bounded revision round; anything unclear escalates to you.")
    print("This is for CODE CHANGES only - success is verified by git diff, "
          "not by the model's answer. For a plain question ('what does this "
          "repo do'), run `opencode run \"<question>\"` directly instead; "
          "routed through here it'll report no changes, which is correct "
          "but easy to misread as an error.")
    print("Enter a goal (add --push to open a PR once QA+PM approve), or 'exit'.")
    while True:
        try:
            line = input("\ngoal> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            return
        if not line or line.lower() in ("exit", "quit"):
            return
        push = False
        if line.endswith("--push"):
            push = True
            line = line[: -len("--push")].strip()
        try:
            run_council(repo, line, model=MODEL_INTERACTIVE, push=push)
        except MissingExecutable as e:
            print(f"ERROR: {e}")


def main():
    args = sys.argv[1:]
    if "--repl" in args:
        args.remove("--repl")
        if len(args) != 1:
            print("usage: chief.py --repl <repo-path>")
            sys.exit(2)
        repl(Path(args[0]))
        return

    push = "--push" in args
    if push:
        args.remove("--push")
    if len(args) < 2:
        print("usage: chief.py <repo-path> <goal> [--push]")
        print("       chief.py --repl <repo-path>")
        sys.exit(2)
    repo = Path(args[0])
    goal = " ".join(args[1:])
    try:
        report = run_council(repo, goal, push=push)
        sys.exit(0 if report["status"] == "APPROVED" else 1)
    except MissingExecutable as e:
        print(f"ERROR: {e}")
        sys.exit(3)


if __name__ == "__main__":
    main()
