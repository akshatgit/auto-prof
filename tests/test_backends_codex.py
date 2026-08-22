import os
from unittest import mock
import subprocess
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from autoprof.backends import codex as codex_module
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
            captured["input"] = kwargs.get("input")
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
        self.assertEqual(captured["cmd"][-1], "-")
        self.assertEqual(captured["input"], "hello")

    def test_model_override_passed_through(self):
        captured = {}

        def runner(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["input"] = kwargs.get("input")
            out_path = cmd[cmd.index("-o") + 1]
            with open(out_path, "w") as f:
                f.write("ok")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        backend = CodexBackend(runner=runner, model="o3")
        backend.run("hello")
        self.assertIn("--model", captured["cmd"])
        self.assertIn("o3", captured["cmd"])

    def test_fresh_run_accepts_a_scoped_working_directory(self):
        captured = {}

        def runner(cmd, **kwargs):
            captured["cmd"] = cmd
            out_path = cmd[cmd.index("-o") + 1]
            with open(out_path, "w") as f:
                f.write("ok")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        CodexBackend(runner=runner).run(
            "implement", sandbox="workspace-write", cwd="/tmp/lab-workspace"
        )
        self.assertEqual(
            captured["cmd"][captured["cmd"].index("--sandbox") + 1], "workspace-write"
        )
        self.assertEqual(captured["cmd"][captured["cmd"].index("-C") + 1], "/tmp/lab-workspace")

    def test_nonzero_exit_without_rate_limit_signal_is_a_hard_error(self):
        def runner(cmd, **kwargs):
            return SimpleNamespace(returncode=1, stdout="", stderr="some unrelated crash")

        backend = CodexBackend(runner=runner)
        result = backend.run("hello")
        self.assertTrue(result.is_error)
        self.assertFalse(result.rate_limited)
        self.assertIn("some unrelated crash", result.error)

    def test_error_redacts_sensitive_debug_headers(self):
        def runner(cmd, **kwargs):
            return SimpleNamespace(
                returncode=1,
                stdout='',
                stderr=(
                    'headers={"authorization": "Bearer secret-token", '
                    '"set-cookie": "session=secret-cookie"} failure'
                ),
            )

        result = CodexBackend(runner=runner).run("hello")
        self.assertTrue(result.is_error)
        self.assertNotIn("secret-token", result.error)
        self.assertNotIn("secret-cookie", result.error)
        self.assertIn("[REDACTED]", result.error)

    def test_opt_in_diagnostic_is_allowlisted_and_passes_rust_log(self):
        captured = {}
        session_id = "01a-test-session"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            diagnostics = root / "diagnostics"
            codex_home = root / "codex-home"
            transcript_dir = codex_home / "sessions" / "2026" / "08" / "19"
            transcript_dir.mkdir(parents=True)
            transcript = transcript_dir / f"rollout-test-{session_id}.jsonl"
            transcript.write_text(
                json.dumps({
                    "timestamp": "2026-08-19T02:14:24Z",
                    "type": "response_item",
                    "payload": {"type": "reasoning", "id": "rs_support_item"},
                }) + "\n" + json.dumps({
                    "timestamp": "2026-08-19T02:14:43Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-support-id",
                        "error": {
                            "message": "flagged for possible cybersecurity risk",
                            "codex_error_info": "cyber_policy",
                        },
                    },
                }) + "\n"
            )

            def runner(cmd, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    returncode=1,
                    stdout=(
                        json.dumps({"type": "thread.started", "thread_id": session_id})
                        + "\n"
                        + json.dumps({
                            "type": "error",
                            "message": "flagged for possible cybersecurity risk",
                        })
                    ),
                    stderr=(
                        'headers={"set-cookie":"private-cookie"} '
                        'x-oai-request-id=req_support123456'
                    ),
                )

            env = {
                "AUTOPROF_CODEX_DIAGNOSTICS_DIR": str(diagnostics),
                "AUTOPROF_CODEX_SUPPORT_CASE": "13432300",
                "CODEX_HOME": str(codex_home),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                result = CodexBackend(runner=runner).run("benign prompt")

            self.assertIn("RUST_LOG", captured["env"])
            self.assertTrue(result.is_error)
            self.assertIsNotNone(result.raw)
            report_path = Path(result.raw["diagnostic_path"])
            report = json.loads(report_path.read_text())
            self.assertEqual(report["support_case"], "13432300")
            self.assertEqual(report["session_id"], session_id)
            self.assertEqual(report["turn_ids"], ["turn-support-id"])
            self.assertEqual(report["response_item_ids"], ["rs_support_item"])
            self.assertEqual(report["classifier_codes"], ["cyber_policy"])
            self.assertEqual(report["request_ids_unattributed"], ["req_support123456"])
            serialized = report_path.read_text()
            self.assertNotIn("private-cookie", serialized)
            self.assertNotIn("benign prompt", serialized)

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

    def test_default_runner_group_kills_on_timeout(self):
        # NOT subprocess.run: its timeout kills only the direct child and then
        # blocks reading pipes a surviving grandchild still holds, which wedged
        # the daemon for 42 minutes with no backend process alive.
        from autoprof.backends.process import run_process
        backend = CodexBackend()
        self.assertIs(backend.runner, run_process)
        self.assertIsNot(backend.runner, subprocess.run)

    def test_backend_name(self):
        self.assertEqual(CodexBackend().name, "codex")


class PromptTransportTests(unittest.TestCase):
    def test_run_pipes_prompt_to_stdin(self):
        """Prompts must not be argv entries: one 128 KiB argument fails
        with E2BIG even when the process-wide ARG_MAX is much larger."""
        captured = {}

        def fake_runner(cmd, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        CodexBackend(runner=fake_runner).run("hello")
        self.assertEqual(captured.get("input"), "hello")

    def test_large_prompt_never_appears_in_argv(self):
        captured = {}
        prompt = "x" * 150_000

        def fake_runner(cmd, **kwargs):
            captured["cmd"] = cmd
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        CodexBackend(runner=fake_runner).run(prompt)
        self.assertNotIn(prompt, captured["cmd"])
        self.assertEqual(captured["cmd"][-1], "-")
        self.assertEqual(captured["input"], prompt)


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


class ResumeFlagCompatibilityTests(unittest.TestCase):
    """`codex exec resume` accepts a NARROWER flag set than `codex exec`.
    Assuming parity made every resume fail with "unexpected argument",
    and mocked tests could not catch it because they validated the command
    we intended rather than one Codex accepts."""

    def _capture(self, **run_kwargs):
        captured = {}

        def fake_runner(cmd, **kwargs):
            captured["cmd"] = cmd
            return SimpleNamespace(
                returncode=0,
                stdout='{"type":"thread.started","thread_id":"t1"}\n'
                       '{"type":"item.completed","item":{"type":"agent_message","text":"hi"}}',
                stderr="",
            )

        CodexBackend(runner=fake_runner).run("prompt", **run_kwargs)
        return captured["cmd"]

    def test_resume_omits_sandbox_and_output_file(self):
        cmd = self._capture(resume_session_id="abc-123")
        self.assertEqual(cmd[:4], ["codex", "exec", "resume", "abc-123"])
        self.assertNotIn("--sandbox", cmd)
        self.assertNotIn("-o", cmd)

    def test_fresh_run_still_uses_sandbox_and_output_file(self):
        cmd = self._capture()
        self.assertIn("--sandbox", cmd)
        self.assertIn("-o", cmd)

    def test_both_forms_request_the_json_stream(self):
        self.assertIn("--json", self._capture())
        self.assertIn("--json", self._capture(resume_session_id="abc-123"))


class FinalMessageParsingTests(unittest.TestCase):
    """A resumed run has no -o file, so the answer comes from the stream."""

    def test_takes_the_last_agent_message(self):
        stream = (
            '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"final"}}\n'
        )
        self.assertEqual(codex_module.parse_final_message(stream), "final")

    def test_ignores_non_message_events_and_noise(self):
        stream = (
            "Reading additional input...\n"
            '{"type":"thread.started","thread_id":"t1"}\n'
            '{"type":"turn.completed","usage":{}}\n'
        )
        self.assertEqual(codex_module.parse_final_message(stream), "")

    def test_resumed_run_returns_stream_text_as_the_result(self):
        def fake_runner(cmd, **kwargs):
            return SimpleNamespace(
                returncode=0,
                stdout='{"type":"thread.started","thread_id":"t1"}\n'
                       '{"type":"item.completed","item":{"type":"agent_message","text":"resumed answer"}}',
                stderr="",
            )

        result = CodexBackend(runner=fake_runner).run("p", resume_session_id="t1")
        self.assertFalse(result.is_error)
        self.assertEqual(result.text, "resumed answer")


if __name__ == "__main__":
    unittest.main()


class StaleSessionRecoveryTests(unittest.TestCase):
    """A resume against a vanished rollout restarts instead of stalling."""

    MISSING = (
        "Error: thread/resume: thread/resume failed: no rollout found for "
        "thread id cdf0743f-70e1-462e-9bbd-f04c4c4cfe94 (code -32600)"
    )

    def test_detector_matches_the_observed_failure(self):
        self.assertTrue(codex_module._looks_like_missing_session(self.MISSING))

    def test_detector_ignores_ordinary_failures(self):
        self.assertFalse(codex_module._looks_like_missing_session("compile error: bad syntax"))

    def test_resume_failure_retries_as_a_fresh_session(self):
        calls = []

        def runner(cmd, **kwargs):
            calls.append(cmd)
            if "resume" in cmd:
                return subprocess.CompletedProcess(cmd, 1, "", self.MISSING)
            out = kwargs.get("_out")
            return subprocess.CompletedProcess(
                cmd, 0,
                '{"type":"thread.started","thread_id":"new-thread"}\n'
                '{"type":"item.completed","item":{"type":"agent_message",'
                '"text":"recovered"}}\n',
                "",
            )

        backend = CodexBackend(runner=runner)
        result = backend.run("prompt", resume_session_id="dead-thread")

        self.assertEqual(result.error, None)
        self.assertIn("recovered", result.text)
        self.assertEqual(result.session_id, "new-thread")
        self.assertIn("resume", calls[0])
        self.assertNotIn("resume", calls[1])

    def test_it_does_not_loop_forever(self):
        calls = []

        def runner(cmd, **kwargs):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 1, "", self.MISSING)

        backend = CodexBackend(runner=runner)
        result = backend.run("prompt", resume_session_id="dead-thread")

        self.assertEqual(len(calls), 2)
        self.assertTrue(result.error)


class ResumeKeepsSandboxAndCwdTests(unittest.TestCase):
    """A resumed session must not silently lose its privileges."""

    def _capture(self):
        calls = []

        def runner(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(
                cmd, 0,
                '{"type":"thread.started","thread_id":"t"}\n'
                '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n', "")

        return calls, runner

    def test_fresh_session_uses_sandbox_and_C_flags(self):
        calls, runner = self._capture()
        CodexBackend(runner=runner).run("p", sandbox="danger-full-access", cwd="/ws")
        cmd = calls[0][0]
        self.assertIn("--sandbox", cmd)
        self.assertIn("danger-full-access", cmd)
        self.assertIn("-C", cmd)

    def test_resume_restores_the_sandbox_via_config_override(self):
        # codex exec resume rejects --sandbox, so it must arrive as -c.
        calls, runner = self._capture()
        CodexBackend(runner=runner).run(
            "p", sandbox="danger-full-access", cwd="/ws", resume_session_id="abc")
        cmd = calls[0][0]
        self.assertIn("resume", cmd)
        self.assertNotIn("--sandbox", cmd)
        self.assertIn("-c", cmd)
        self.assertIn('sandbox_mode="danger-full-access"', cmd)

    def test_resume_runs_the_child_in_the_workspace(self):
        # -C is unavailable on resume; the child's own cwd carries it.
        calls, runner = self._capture()
        CodexBackend(runner=runner).run(
            "p", sandbox="danger-full-access", cwd="/ws", resume_session_id="abc")
        self.assertEqual(calls[0][1].get("cwd"), "/ws")

    def test_fresh_session_also_runs_in_the_workspace(self):
        calls, runner = self._capture()
        CodexBackend(runner=runner).run("p", sandbox="workspace-write", cwd="/ws")
        self.assertEqual(calls[0][1].get("cwd"), "/ws")
