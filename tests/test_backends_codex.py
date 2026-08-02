import os
from unittest import mock
import subprocess
import unittest
from types import SimpleNamespace

from autoprof.backends.codex import CodexBackend, parse_session_id


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


class StdinIsClosedTests(unittest.TestCase):
    def test_run_passes_devnull_as_stdin(self):
        """`codex exec` reads extra prompt input from stdin; an inherited
        stdin makes it block until the timeout expires (see codex.py)."""
        captured = {}

        def fake_runner(cmd, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        CodexBackend(runner=fake_runner).run("hello")
        self.assertEqual(captured.get("stdin"), subprocess.DEVNULL)


class NoWallClockLimitTests(unittest.TestCase):
    def test_default_timeout_is_none(self):
        """The 900s ceiling was ours, not Codex's, and it killed live jobs
        mid-derivation. Default is now no limit."""
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(CodexBackend().timeout)

    def test_env_var_can_reinstate_a_timeout(self):
        with mock.patch.dict(os.environ, {"AUTOPROF_CODEX_TIMEOUT": "120"}, clear=True):
            self.assertEqual(CodexBackend().timeout, 120.0)

    def test_explicit_none_stays_none(self):
        with mock.patch.dict(os.environ, {"AUTOPROF_CODEX_TIMEOUT": "120"}, clear=True):
            self.assertIsNone(CodexBackend(timeout=None).timeout)

    def test_timeout_none_is_passed_through_to_the_runner(self):
        captured = {}

        def fake_runner(cmd, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.dict(os.environ, {}, clear=True):
            CodexBackend(runner=fake_runner).run("hi")
        self.assertIsNone(captured.get("timeout"))


class SessionResumeTests(unittest.TestCase):
    _EVENTS = (
        '{"type":"thread.started","thread_id":"abc-123"}\n'
        '{"type":"turn.completed","usage":{"output_tokens":5}}\n'
    )

    def test_parses_thread_id_from_json_events(self):
        self.assertEqual(parse_session_id(self._EVENTS), "abc-123")

    def test_ignores_non_json_noise(self):
        noisy = "Reading additional input...\n" + self._EVENTS
        self.assertEqual(parse_session_id(noisy), "abc-123")

    def test_missing_id_degrades_to_none_not_a_crash(self):
        self.assertIsNone(parse_session_id("no events here"))

    def test_session_id_returned_on_success(self):
        def fake_runner(cmd, **kwargs):
            return SimpleNamespace(returncode=0, stdout=self._EVENTS, stderr="")

        result = CodexBackend(runner=fake_runner).run("hi")
        self.assertEqual(result.session_id, "abc-123")

    def test_session_id_returned_on_error_so_retry_can_resume(self):
        def fake_runner(cmd, **kwargs):
            return SimpleNamespace(returncode=1, stdout=self._EVENTS, stderr="boom")

        result = CodexBackend(runner=fake_runner).run("hi")
        self.assertTrue(result.is_error)
        self.assertEqual(result.session_id, "abc-123")

    def test_token_exhaustion_is_rate_limited_not_error(self):
        """Token exhaustion must not burn a retry attempt, and must keep
        the session so the next attempt continues the derivation."""
        def fake_runner(cmd, **kwargs):
            return SimpleNamespace(
                returncode=1,
                stdout=self._EVENTS,
                stderr="Error: maximum context length exceeded",
            )

        result = CodexBackend(runner=fake_runner).run("hi")
        self.assertTrue(result.rate_limited)
        self.assertFalse(result.is_error)
        self.assertEqual(result.session_id, "abc-123")

    def test_resume_session_id_builds_a_resume_command(self):
        captured = {}

        def fake_runner(cmd, **kwargs):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout=self._EVENTS, stderr="")

        CodexBackend(runner=fake_runner).run("hi", resume_session_id="abc-123")
        cmd = captured["cmd"]
        self.assertEqual(cmd[:4], ["codex", "exec", "resume", "abc-123"])

    def test_no_resume_id_means_a_fresh_exec(self):
        captured = {}

        def fake_runner(cmd, **kwargs):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0, stdout=self._EVENTS, stderr="")

        CodexBackend(runner=fake_runner).run("hi")
        self.assertEqual(captured["cmd"][:2], ["codex", "exec"])
        self.assertNotIn("resume", captured["cmd"])


class EmptyOutputTests(unittest.TestCase):
    """A clean exit with no output is a failure. Treating it as an empty
    success let a killed run silently erase a student's memory.md."""

    _EVENTS = '{"type":"thread.started","thread_id":"abc-123"}\n'

    def test_zero_exit_with_no_output_is_an_error(self):
        def fake_runner(cmd, **kwargs):
            return SimpleNamespace(returncode=0, stdout=self._EVENTS, stderr="")

        result = CodexBackend(runner=fake_runner).run("hi")
        self.assertTrue(result.is_error)
        self.assertIn("no output", result.error)

    def test_whitespace_only_output_is_an_error(self):
        result = CodexBackend(
            runner=fake_runner_writing_output("   \n\t ")
        ).run("hi")
        self.assertTrue(result.is_error)

    def test_empty_output_still_reports_session_for_resume(self):
        def fake_runner(cmd, **kwargs):
            return SimpleNamespace(returncode=0, stdout=self._EVENTS, stderr="")

        result = CodexBackend(runner=fake_runner).run("hi")
        self.assertEqual(result.session_id, "abc-123")

    def test_real_output_is_still_success(self):
        result = CodexBackend(runner=fake_runner_writing_output("actual answer")).run("hi")
        self.assertFalse(result.is_error)
        self.assertEqual(result.text, "actual answer")


if __name__ == "__main__":
    unittest.main()
