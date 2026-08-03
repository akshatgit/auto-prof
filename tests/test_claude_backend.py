"""Tests for the Claude CLI backend and the mixed review panel.

The panel tests matter more than the backend tests: a panel that silently
collapses to one model family still passes every review gate in the
system while making the vote meaningless.
"""

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof.backends import registry  # noqa: E402
from autoprof.backends.claude_cli import ClaudeBackend, parse_result  # noqa: E402


def fake_run(stdout="", stderr="", returncode=0, capture=None):
    def runner(cmd, **kwargs):
        if capture is not None:
            capture.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
    return runner


def ok_payload(text="VERDICT: reject", session="sess-1"):
    return f'{{"is_error":false,"result":{text!r},"session_id":"{session}"}}'.replace("'", '"')


class ParseResultTests(unittest.TestCase):
    def test_extracts_text_and_session(self):
        text, session, is_error = parse_result(ok_payload())
        self.assertEqual(text, "VERDICT: reject")
        self.assertEqual(session, "sess-1")
        self.assertFalse(is_error)

    def test_session_survives_an_errored_payload(self):
        """The session id is needed precisely when the run failed."""
        _, session, is_error = parse_result('{"is_error":true,"result":"boom","session_id":"s9"}')
        self.assertEqual(session, "s9")
        self.assertTrue(is_error)

    def test_unparseable_stdout_is_an_error_not_empty_success(self):
        _, _, is_error = parse_result("not json at all")
        self.assertTrue(is_error)
        self.assertTrue(parse_result("")[2])

    def test_tolerates_leading_noise_before_the_json(self):
        text, _, _ = parse_result("warning: something\n" + ok_payload())
        self.assertEqual(text, "VERDICT: reject")


class ClaudeBackendTests(unittest.TestCase):
    def test_builds_a_read_only_print_command(self):
        calls = []
        ClaudeBackend(runner=fake_run(ok_payload(), capture=calls)).run("judge this")
        cmd, kwargs = calls[0]
        self.assertEqual(cmd[:4], ["claude", "-p", "--output-format", "json"])
        self.assertIn("--permission-mode", cmd)
        # A reviewer that can edit could 'fix' the paper it is judging.
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "plan")
        self.assertEqual(cmd[-1], "judge this")
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)

    def test_resume_passes_the_session_id(self):
        calls = []
        ClaudeBackend(runner=fake_run(ok_payload(), capture=calls)).run(
            "go on", resume_session_id="abc"
        )
        cmd, _ = calls[0]
        self.assertIn("--resume", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc")

    def test_rate_limit_is_not_an_error(self):
        result = ClaudeBackend(
            runner=fake_run("", "Claude usage limit reached, try again in 5m", 1)
        ).run("x")
        self.assertTrue(result.rate_limited)
        self.assertFalse(result.is_error)   # must not burn a retry attempt
        self.assertEqual(result.retry_after_seconds, 300.0)

    def test_context_exhaustion_is_treated_as_rate_limited(self):
        result = ClaudeBackend(runner=fake_run("", "prompt is too long", 1)).run("x")
        self.assertTrue(result.rate_limited)
        self.assertFalse(result.is_error)

    def test_clean_exit_with_no_text_is_a_failure(self):
        """Callers write results over memory files; empty success erases them."""
        result = ClaudeBackend(
            runner=fake_run('{"is_error":false,"result":"","session_id":"s1"}')
        ).run("x")
        self.assertTrue(result.is_error)
        self.assertEqual(result.session_id, "s1")

    def test_missing_cli_is_reported_not_raised(self):
        def missing(cmd, **kwargs):
            raise FileNotFoundError
        result = ClaudeBackend(runner=missing).run("x")
        self.assertIn("not found", result.error)

    def test_timeout_keeps_the_session_for_the_retry(self):
        def slow(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 5, output=ok_payload(session="s7"))
        result = ClaudeBackend(timeout=5, runner=slow).run("x")
        self.assertTrue(result.is_error)
        self.assertEqual(result.session_id, "s7")

    def test_no_wall_clock_limit_by_default(self):
        """Our own deadline would discard work already paid for."""
        calls = []
        ClaudeBackend(runner=fake_run(ok_payload(), capture=calls)).run("x")
        self.assertIsNone(calls[0][1]["timeout"])


class ReviewPanelTests(unittest.TestCase):
    def test_the_default_panel_is_not_one_family(self):
        """The whole point: 3 processes of one model are not 3 opinions."""
        names = {
            registry.resolve_backend_name("paper_review", {}, {}, i) for i in (1, 2, 3)
        }
        self.assertGreater(len(names), 1)
        self.assertIn("claude", names)
        self.assertIn("codex", names)

    def test_a_five_member_defense_panel_stays_mixed(self):
        names = [
            registry.resolve_backend_name("defense_review", {}, {}, i) for i in range(1, 6)
        ]
        self.assertEqual(len(set(names)), 2)   # cycles, never collapses

    def test_the_same_reviewer_index_is_stable(self):
        first = registry.resolve_backend_name("paper_review", {}, {}, 2)
        self.assertEqual(first, registry.resolve_backend_name("paper_review", {}, {}, 2))

    def test_env_overrides_the_panel(self):
        env = {"AUTOPROF_REVIEW_PANEL": "claude, codex, claude"}
        self.assertEqual(registry.resolve_backend_name("paper_review", {}, env, 1), "claude")
        self.assertEqual(registry.resolve_backend_name("paper_review", {}, env, 2), "codex")

    def test_config_sets_the_panel_when_env_is_silent(self):
        config = {"backends": {"review_panel": ["claude", "claude"]}}
        self.assertEqual(registry.resolve_backend_name("paper_review", config, {}, 1), "claude")

    def test_an_explicit_per_kind_pin_still_beats_the_panel(self):
        env = {"AUTOPROF_BACKEND_PAPER_REVIEW": "codex"}
        self.assertEqual(registry.resolve_backend_name("paper_review", {}, env, 2), "codex")

    def test_generation_kinds_ignore_the_reviewer_index(self):
        self.assertEqual(
            registry.resolve_backend_name("student_work", {}, {}, 2), "ollama_cloud"
        )

    def test_no_reviewer_index_falls_back_to_the_category_default(self):
        self.assertEqual(registry.resolve_backend_name("paper_review", {}, {}), "codex")

    def test_registry_hands_out_the_panel_backend(self):
        reg = registry.Registry(env={})
        self.assertEqual(reg.get_backend("paper_review", 1).name, "codex")
        self.assertEqual(reg.get_backend("paper_review", 2).name, "claude")


if __name__ == "__main__":
    unittest.main()
