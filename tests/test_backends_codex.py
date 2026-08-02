import subprocess
import unittest
from types import SimpleNamespace

from autoprof.backends.codex import CodexBackend


def fake_runner_writing_output(output_text, returncode=0, stderr=""):
    """Build a fake `runner(cmd, **kw)` that mimics `codex exec -o <file>`
    by writing to whatever path follows '-o' in the command, so tests
    never touch a real subprocess or real filesystem outside a tmp path
    the test itself controls via tmp_path fixtures."""

    def runner(cmd, **kwargs):
        if "-o" in cmd:
            out_path = cmd[cmd.index("-o") + 1]
            with open(out_path, "w") as f:
                f.write(output_text)
        return SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    return runner


class CodexBackendTests(unittest.TestCase):
    def test_successful_run_reads_output_file(self):
        backend = CodexBackend(runner=fake_runner_writing_output("strong_accept, looks good"))
        result = backend.run("review this paper")
        self.assertEqual(result.text, "strong_accept, looks good")
        self.assertFalse(result.is_error)
        self.assertFalse(result.rate_limited)

    def test_command_includes_skip_git_repo_check_and_sandbox(self):
        captured = {}

        def runner(cmd, **kwargs):
            captured["cmd"] = cmd
            out_path = cmd[cmd.index("-o") + 1]
            with open(out_path, "w") as f:
                f.write("ok")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        backend = CodexBackend(runner=runner)
        backend.run("hello")
        self.assertIn("--skip-git-repo-check", captured["cmd"])
        self.assertIn("--sandbox", captured["cmd"])
        self.assertIn("codex", captured["cmd"])
        self.assertIn("exec", captured["cmd"])
        self.assertIn("hello", captured["cmd"])

    def test_model_override_passed_through(self):
        captured = {}

        def runner(cmd, **kwargs):
            captured["cmd"] = cmd
            out_path = cmd[cmd.index("-o") + 1]
            with open(out_path, "w") as f:
                f.write("ok")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        backend = CodexBackend(runner=runner, model="o3")
        backend.run("hello")
        self.assertIn("--model", captured["cmd"])
        self.assertIn("o3", captured["cmd"])

    def test_nonzero_exit_without_rate_limit_signal_is_a_hard_error(self):
        def runner(cmd, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="some unrelated crash")

        backend = CodexBackend(runner=runner)
        result = backend.run("hello")
        self.assertTrue(result.is_error)
        self.assertFalse(result.rate_limited)
        self.assertIn("some unrelated crash", result.error)

    def test_rate_limit_signal_sets_rate_limited_not_error(self):
        def runner(cmd, **kwargs):
            return SimpleNamespace(
                returncode=1, stdout="", stderr="rate limited, try again in 45s"
            )

        backend = CodexBackend(runner=runner)
        result = backend.run("hello")
        self.assertTrue(result.rate_limited)
        self.assertFalse(result.is_error)
        self.assertEqual(result.retry_after_seconds, 45.0)

    def test_rate_limit_minutes_parsed(self):
        def runner(cmd, **kwargs):
            return SimpleNamespace(
                returncode=1, stdout="", stderr="usage limit reached, try again in 3m"
            )

        backend = CodexBackend(runner=runner)
        result = backend.run("hello")
        self.assertTrue(result.rate_limited)
        self.assertEqual(result.retry_after_seconds, 180.0)

    def test_rate_limit_without_explicit_duration_still_flagged(self):
        def runner(cmd, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="Error: rate limit exceeded")

        backend = CodexBackend(runner=runner)
        result = backend.run("hello")
        self.assertTrue(result.rate_limited)
        self.assertIsNone(result.retry_after_seconds)

    def test_timeout_is_reported_as_error_not_raised(self):
        def runner(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=kwargs.get("timeout", 0))

        backend = CodexBackend(runner=runner, timeout=5)
        result = backend.run("hello")
        self.assertTrue(result.is_error)
        self.assertIn("timed out", result.error.lower())

    def test_default_runner_is_subprocess_run(self):
        # Sanity check the production default without actually invoking it.
        backend = CodexBackend()
        self.assertIs(backend.runner, subprocess.run)

    def test_backend_name(self):
        self.assertEqual(CodexBackend().name, "codex")


if __name__ == "__main__":
    unittest.main()
