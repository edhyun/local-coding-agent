"""Tier 1 + Tier 2 tests for orchestrator.py (see /plan-eng-review, 2026-08-26).

Tier 1: pure/deterministic logic, no subprocess or model dependency.
Tier 2: subprocess-dependent (git/gh) but testable against a real scratch git
repo fixture, no live model needed.

Tier 3 (invoke_opencode, run_task, repl, main - anything that shells out to a
live local model) is deliberately not covered here - see TODOS.md.
"""

import json
import shutil
import unittest.mock
import subprocess
import tempfile
import unittest
from pathlib import Path

import orchestrator


def make_scratch_repo() -> Path:
    d = Path(tempfile.mkdtemp(prefix="orchestrator-test-"))
    subprocess.run(["git", "init", "-q"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.email", "test@test.local"], cwd=d, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=d, check=True)
    (d / "README.md").write_text("scratch\n")
    subprocess.run(["git", "add", "-A"], cwd=d, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=d, check=True)
    return d


class TestSlugify(unittest.TestCase):
    def test_empty_string(self):
        self.assertEqual(orchestrator.slugify(""), "task")

    def test_all_punctuation(self):
        self.assertEqual(orchestrator.slugify("!!!???"), "task")

    def test_long_task_truncated_to_40_chars(self):
        task = "a" * 100
        self.assertEqual(len(orchestrator.slugify(task)), 40)

    def test_unicode_input_falls_back_to_task(self):
        # non-ascii-alnum characters all get stripped to separators
        self.assertEqual(orchestrator.slugify("日本語"), "task")

    def test_normal_task_slugified(self):
        self.assertEqual(
            orchestrator.slugify("Add a Docstring to make_reader()!"),
            "add-a-docstring-to-make-reader",
        )


class TestTaskHash(unittest.TestCase):
    def test_same_task_same_hash(self):
        task = "Add a one-line docstring to the top of council/config.py"
        self.assertEqual(orchestrator.task_hash(task), orchestrator.task_hash(task))

    def test_different_tasks_that_share_a_slugified_prefix_get_different_hashes(self):
        """The actual bug found live (2026-08-27): two different tasks
        whose first 40 chars are identical must not collide on branch
        name."""
        task_a = "Add a one-line docstring to the top of council/config.py explaining what it configures."
        task_b = "Add a one-line docstring to the top of council/observability.py explaining what it does."
        self.assertEqual(orchestrator.slugify(task_a), orchestrator.slugify(task_b))
        self.assertNotEqual(orchestrator.task_hash(task_a), orchestrator.task_hash(task_b))


class TestPermissionProfiles(unittest.TestCase):
    def setUp(self):
        self.repo = make_scratch_repo()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_engineer_profile_allows_edit(self):
        orchestrator.ensure_permission_config(self.repo, profile="engineer")
        config = json.loads((self.repo / "opencode.jsonc").read_text())
        self.assertEqual(config["permission"]["edit"], "allow")

    def test_reviewer_profile_denies_edit(self):
        orchestrator.ensure_permission_config(self.repo, profile="reviewer")
        config = json.loads((self.repo / "opencode.jsonc").read_text())
        self.assertEqual(config["permission"]["edit"], "deny")
        self.assertEqual(config["permission"]["read"], "allow")

    def test_always_overwritten_not_skipped_if_present(self):
        orchestrator.ensure_permission_config(self.repo, profile="reviewer")
        orchestrator.ensure_permission_config(self.repo, profile="engineer")
        config = json.loads((self.repo / "opencode.jsonc").read_text())
        self.assertEqual(config["permission"]["edit"], "allow")


class TestGitExclude(unittest.TestCase):
    def setUp(self):
        self.repo = make_scratch_repo()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_idempotent(self):
        orchestrator.ensure_git_exclude(self.repo)
        first = (self.repo / ".git" / "info" / "exclude").read_text()
        orchestrator.ensure_git_exclude(self.repo)
        second = (self.repo / ".git" / "info" / "exclude").read_text()
        self.assertEqual(first, second)

    def test_registers_agent_files_and_common_junk(self):
        orchestrator.ensure_git_exclude(self.repo)
        content = (self.repo / ".git" / "info" / "exclude").read_text()
        for entry in orchestrator.AGENT_FILES + orchestrator.COMMON_JUNK:
            self.assertIn(entry, content)


class TestDetectTestCommand(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="orchestrator-test-"))

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_uv_lock_and_pyproject(self):
        (self.repo / "uv.lock").write_text("")
        (self.repo / "pyproject.toml").write_text("")
        self.assertEqual(orchestrator.detect_test_command(self.repo), ["uv", "run", "pytest"])

    def test_pyproject_alone(self):
        (self.repo / "pyproject.toml").write_text("")
        self.assertEqual(orchestrator.detect_test_command(self.repo), ["pytest"])

    def test_package_json_with_test_script(self):
        (self.repo / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
        self.assertEqual(orchestrator.detect_test_command(self.repo), ["npm", "test"])

    def test_package_json_without_test_script(self):
        (self.repo / "package.json").write_text(json.dumps({"scripts": {}}))
        self.assertIsNone(orchestrator.detect_test_command(self.repo))

    def test_cargo_toml(self):
        (self.repo / "Cargo.toml").write_text("")
        self.assertEqual(orchestrator.detect_test_command(self.repo), ["cargo", "test"])

    def test_go_mod(self):
        (self.repo / "go.mod").write_text("")
        self.assertEqual(orchestrator.detect_test_command(self.repo), ["go", "test", "./..."])

    def test_nothing_detected(self):
        self.assertIsNone(orchestrator.detect_test_command(self.repo))


class TestQueueCrashResume(unittest.TestCase):
    def setUp(self):
        self.repo = make_scratch_repo()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_running_entry_is_picked_up_not_skipped(self):
        """D6's core promise: a 'running' entry (simulating a mid-task crash)
        must be resumed, not treated as already handled."""
        paths = orchestrator.enqueue_tasks(self.repo, ["task one"])
        entry = orchestrator.load_queue_entry(paths[0])
        entry["status"] = "running"
        orchestrator.save_queue_entry(paths[0], entry)

        pending = orchestrator.pending_queue_entries(self.repo)
        self.assertIn(paths[0], pending)

    def test_done_entry_is_not_picked_up(self):
        paths = orchestrator.enqueue_tasks(self.repo, ["task one"])
        entry = orchestrator.load_queue_entry(paths[0])
        entry["status"] = "done"
        orchestrator.save_queue_entry(paths[0], entry)

        pending = orchestrator.pending_queue_entries(self.repo)
        self.assertNotIn(paths[0], pending)

    def test_pending_entry_is_picked_up(self):
        paths = orchestrator.enqueue_tasks(self.repo, ["task one"])
        pending = orchestrator.pending_queue_entries(self.repo)
        self.assertIn(paths[0], pending)


class TestCommitChanges(unittest.TestCase):
    def setUp(self):
        self.repo = make_scratch_repo()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_commit_message_truncates_to_72_chars(self):
        (self.repo / "new.txt").write_text("x")
        long_task = "x" * 200
        orchestrator.commit_changes(self.repo, long_task)
        log = subprocess.run(
            ["git", "log", "-1", "--pretty=%s"], cwd=self.repo, capture_output=True, text=True
        ).stdout.strip()
        # "Agent: " prefix (7 chars) + up to 72 chars of the task
        self.assertLessEqual(len(log), 7 + 72)
        self.assertTrue(log.startswith("Agent: "))

    def test_returns_correct_commit_sha(self):
        (self.repo / "new.txt").write_text("x")
        sha = orchestrator.commit_changes(self.repo, "a task")
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, text=True
        ).stdout.strip()
        self.assertEqual(sha, head)


class TestPushAndOpenPrGateRefusal(unittest.TestCase):
    def setUp(self):
        self.repo = make_scratch_repo()

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_refuses_immediately_when_gate_failed(self):
        """No git/gh command should even be attempted when the safety gate
        already failed - this branch is pure logic, needs no real push."""
        result = orchestrator.push_and_open_pr(
            self.repo, "some-branch", "a task", {"status": "FAIL"}
        )
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "safety gate failed")


class TestPrintEventLive(unittest.TestCase):
    def _capture(self, event):
        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            orchestrator.print_event_live(event)
        return buf.getvalue()

    def test_text_event_prints_text(self):
        out = self._capture({"type": "text", "part": {"text": "hello world"}})
        self.assertIn("hello world", out)

    def test_write_tool_prints_file_path(self):
        out = self._capture({
            "type": "tool_use",
            "part": {
                "tool": "write",
                "state": {"status": "completed", "input": {"filePath": "foo.py"}},
            },
        })
        self.assertIn("foo.py", out)
        self.assertIn("write", out)

    def test_error_status_prints_denial_reason(self):
        out = self._capture({
            "type": "tool_use",
            "part": {
                "tool": "bash",
                "state": {"status": "error", "error": "denied: external directory",
                          "input": {"command": "ls /"}},
            },
        })
        self.assertIn("denied", out)

    def test_unrecognized_event_type_does_not_crash(self):
        # should simply produce no output, not raise
        out = self._capture({"type": "something_unknown"})
        self.assertEqual(out, "")


class TestClassifyIntent(unittest.TestCase):
    def test_unknown_provider_fails_toward_code(self):
        """A wrong classification must fail toward the existing, already-
        verified pipeline (CODE) rather than a new, less-tested path."""
        self.assertEqual(
            orchestrator.classify_intent("hello", "some-unknown-provider/model"),
            "CODE",
        )

    def test_network_error_fails_toward_code(self):
        with unittest.mock.patch(
            "orchestrator.urllib.request.urlopen",
            side_effect=orchestrator.urllib.error.URLError("connection refused"),
        ):
            self.assertEqual(
                orchestrator.classify_intent("hello", "ollama/qwen3-coder:30b"),
                "CODE",
            )

    def test_question_classification_parsed_ollama_native_shape(self):
        # Ollama uses the native /api/chat response shape ({"message":
        # {"content": ...}}), not the OpenAI-compatible {"choices": [...]}
        # shape - found live 2026-08-27 when a test using the wrong shape
        # would have masked the real bug (qwen3:8b's reasoning tokens
        # eating the OpenAI-compatible endpoint's whole max_tokens budget).
        response = json.dumps({"message": {"content": "QUESTION"}}).encode()

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return response

        with unittest.mock.patch("orchestrator.urllib.request.urlopen", return_value=FakeResp()):
            self.assertEqual(
                orchestrator.classify_intent("hello", "ollama/qwen3-coder:30b"),
                "QUESTION",
            )

    def test_question_classification_parsed_openai_compatible_shape(self):
        # Non-Ollama providers (e.g. lmstudio) go through the OpenAI-
        # compatible {"choices": [...]} shape, a genuinely different code
        # path from the Ollama-native one above since the fix.
        response = json.dumps({"choices": [{"message": {"content": "QUESTION"}}]}).encode()

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return response

        with unittest.mock.patch("orchestrator.urllib.request.urlopen", return_value=FakeResp()):
            self.assertEqual(
                orchestrator.classify_intent("hello", "lmstudio/qwen3.8-27b-mlx"),
                "QUESTION",
            )

    def test_ollama_request_uses_native_endpoint_with_think_false(self):
        captured = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"message": {"content": "CODE"}}).encode()

        def fake_urlopen(req, timeout=30):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data)
            return FakeResp()

        with unittest.mock.patch("orchestrator.urllib.request.urlopen", side_effect=fake_urlopen):
            orchestrator.classify_intent("hello", "ollama/qwen3:8b")
        self.assertEqual(captured["url"], orchestrator.OLLAMA_NATIVE_CHAT_ENDPOINT)
        self.assertEqual(captured["body"]["think"], False)


if __name__ == "__main__":
    unittest.main()
