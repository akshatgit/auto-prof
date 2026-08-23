"""Tests for the student verifier and visualiser tools."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import tools  # noqa: E402
from tests.helpers import fresh_db, seed_lab_with_student  # noqa: E402


class ParseToolCallsTests(unittest.TestCase):
    def test_extracts_both_tools_in_order(self):
        text = (
            "I will check this.\n"
            "```tool:verify\nprint(1)\n```\n"
            "and plot it\n"
            '```tool:visualize\n{"kind":"line"}\n```\n'
        )
        calls = tools.parse_tool_calls(text)
        self.assertEqual([t for t, _ in calls], ["verify", "visualize"])
        self.assertIn("print(1)", calls[0][1])

    def test_no_calls_is_empty(self):
        self.assertEqual(tools.parse_tool_calls("just prose"), [])
        self.assertEqual(tools.parse_tool_calls(""), [])

    def test_calls_are_capped(self):
        text = "```tool:verify\nprint(1)\n```\n" * 10
        self.assertEqual(len(tools.parse_tool_calls(text)), tools.MAX_TOOL_CALLS_PER_ROUND)

    def test_accepts_xml_transport_used_by_compatible_backends(self):
        text = (
            "<tool:readfile><path>cacheprobe/canonicalize.py</path></tool:readfile>\n"
            "then test it\n"
            "```tool:experiment\n{\"command\":[\"./run_tests.sh\"]}\n```"
        )
        self.assertEqual(
            tools.parse_tool_calls(text),
            [
                ("readfile", "cacheprobe/canonicalize.py"),
                ("experiment", '{"command":["./run_tests.sh"]}\n'),
            ],
        )

    def test_xml_closing_tag_must_match_the_opening_tool(self):
        text = "<tool:readfile>safe.txt</tool:experiment>"
        self.assertEqual(tools.parse_tool_calls(text), [])

    def test_accepts_gateway_wrapper_only_when_it_closes_the_response(self):
        text = (
            "I will patch it.\n```tool:apply_patch\n--- a/a\n+++ b/a\n"
            "</tool_calls>"
        )
        self.assertEqual(
            tools.parse_tool_calls(text),
            [("apply_patch", "--- a/a\n+++ b/a")],
        )
        self.assertEqual(tools.parse_tool_calls(text + " trailing prose"), [])

    def test_detects_apparent_but_unparseable_tool_syntax(self):
        self.assertTrue(tools.has_unparsed_tool_syntax("```tool:readfile\na.txt"))
        self.assertFalse(
            tools.has_unparsed_tool_syntax("<tool:readfile>a.txt</tool:readfile>")
        )

    def test_unwraps_single_argument_xml_but_not_arbitrary_nested_content(self):
        self.assertEqual(
            tools.parse_tool_calls(
                "<tool:readfile><path>cacheprobe/runner.py</path></tool:readfile>"
            ),
            [("readfile", "cacheprobe/runner.py")],
        )
        body = "<root><path>part-of-payload</path></root>"
        self.assertEqual(
            tools.parse_tool_calls(f"<tool:verify>{body}</tool:verify>"),
            [("verify", body)],
        )


class VerifierTests(unittest.TestCase):
    def test_captures_printed_output(self):
        result = tools.run_verifier("print('RESULT: 42')")
        self.assertEqual(result["status"], "ok")
        self.assertIn("RESULT: 42", result["output"])

    def test_a_crashing_program_returns_its_error_rather_than_raising(self):
        """A program that crashes is an informative result the student
        should see, not an exception that kills the job."""
        result = tools.run_verifier("raise ValueError('bad assumption')")
        self.assertEqual(result["status"], "error")
        self.assertIn("bad assumption", result["output"])

    def test_silent_program_is_an_error(self):
        result = tools.run_verifier("x = 1 + 1")
        self.assertEqual(result["status"], "error")
        self.assertIn("printed nothing", result["output"])

    def test_runaway_program_times_out_with_advice(self):
        result = tools.run_verifier("while True: pass", timeout=2)
        self.assertEqual(result["status"], "timeout")
        self.assertIn("reduce the search space", result["output"])

    def test_real_exhaustive_search_works(self):
        """The actual use case: settle a finite claim by enumeration."""
        code = (
            "from itertools import combinations\n"
            "bad = [s for s in combinations(range(6), 3) if sum(s) % 2 == 0]\n"
            "print('RESULT:', len(bad))\n"
        )
        result = tools.run_verifier(code)
        self.assertEqual(result["status"], "ok")
        self.assertIn("RESULT:", result["output"])

    def test_cannot_reach_the_lab_database(self):
        """`-I` isolates the child from this repo's modules, so a program
        cannot import autoprof and touch lab state."""
        result = tools.run_verifier("import autoprof; print(autoprof.__file__)")
        self.assertEqual(result["status"], "error")


class VisualizerTests(unittest.TestCase):
    SPEC = {
        "kind": "step",
        "title": "Exact spectrum",
        "x_label": "epsilon",
        "y_label": "R_r",
        "series": [{"name": "r=4", "points": [[0, 1], [0.5, 0.5], [1, 0.25]]}],
    }

    def test_renders_an_svg_with_labels(self):
        svg = tools.render_chart(self.SPEC)
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("Exact spectrum", svg)
        self.assertIn("epsilon", svg)
        self.assertIn("r=4", svg)  # direct label, not a colour-only legend

    def test_series_get_distinct_colours_and_dashes(self):
        """Identity must survive greyscale printing, so colour is paired
        with a dash pattern."""
        spec = dict(self.SPEC, series=[
            {"name": "a", "points": [[0, 1], [1, 2]]},
            {"name": "b", "points": [[0, 2], [1, 1]]},
        ])
        svg = tools.render_chart(spec)
        self.assertIn(tools.SERIES_COLOURS[0], svg)
        self.assertIn(tools.SERIES_COLOURS[1], svg)
        self.assertIn("stroke-dasharray", svg)

    def test_too_many_series_is_refused(self):
        spec = dict(self.SPEC, series=[
            {"name": f"s{i}", "points": [[0, 1], [1, 2]]}
            for i in range(len(tools.SERIES_COLOURS) + 1)
        ])
        with self.assertRaises(tools.ToolError):
            tools.render_chart(spec)

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(tools.ToolError):
            tools.render_chart(dict(self.SPEC, kind="pie"))

    def test_empty_series_is_refused(self):
        with self.assertRaises(tools.ToolError):
            tools.render_chart({"kind": "line", "series": []})

    def test_labels_are_escaped(self):
        svg = tools.render_chart(dict(self.SPEC, title="<script>x</script>"))
        self.assertNotIn("<script>", svg)
        self.assertIn("&lt;script&gt;", svg)

    def test_bad_json_returns_an_error_not_an_exception(self):
        result = tools.run_visualizer("{not json")
        self.assertEqual(result["status"], "error")
        self.assertIn("not valid JSON", result["output"])

    def test_valid_spec_through_the_json_path(self):
        result = tools.run_visualizer(json.dumps(self.SPEC))
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["output"].startswith("<svg"))


class ExecuteToolCallsTests(unittest.TestCase):
    def test_runs_are_recorded_with_their_artifacts(self):
        """'Verified by exhaustive search' in a paper must be traceable to
        the exact program and its exact output."""
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            block = tools.execute_tool_calls(
                conn,
                [("verify", "print('RESULT: ok')")],
                lab_id=ids["lab_id"],
                task_id=ids["task_id"],
                student_id=ids["student_id"],
                lab_dir=lab_dir,
            )
            row = conn.execute("SELECT * FROM tool_runs").fetchone()
            self.assertEqual(row["tool"], "verify")
            self.assertEqual(row["status"], "ok")
            self.assertTrue((lab_dir / row["input_path"]).exists())
            self.assertTrue((lab_dir / row["output_path"]).exists())
            self.assertIn("RESULT: ok", (lab_dir / row["output_path"]).read_text())
            self.assertIn("RESULT: ok", block)
            self.assertIn("authoritative over your own expectations", block)
        conn.close()

    def test_svg_output_is_saved_as_svg(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            tools.execute_tool_calls(
                conn,
                [("visualize", json.dumps(VisualizerTests.SPEC))],
                lab_id=ids["lab_id"],
                task_id=ids["task_id"],
                student_id=ids["student_id"],
                lab_dir=lab_dir,
            )
            row = conn.execute("SELECT * FROM tool_runs").fetchone()
            self.assertTrue(row["output_path"].endswith(".svg"))
        conn.close()

    def test_no_calls_returns_nothing(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(
                tools.execute_tool_calls(
                    conn, [], lab_id=ids["lab_id"], task_id=ids["task_id"],
                    student_id=ids["student_id"], lab_dir=Path(d),
                ),
                "",
            )
        conn.close()

    def test_a_failing_tool_is_still_recorded(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            tools.execute_tool_calls(
                conn, [("verify", "raise SystemExit(3)")],
                lab_id=ids["lab_id"], task_id=ids["task_id"],
                student_id=ids["student_id"], lab_dir=Path(d),
            )
            self.assertEqual(
                conn.execute("SELECT status FROM tool_runs").fetchone()[0], "error"
            )
        conn.close()


class RepoToolTests(unittest.TestCase):
    """A lab whose subject is a codebase needs to read it -- and must not
    be able to edit the daemon running it."""

    def test_readfile_needs_a_configured_repository(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            result = tools.run_readfile("some/file.py")
        self.assertEqual(result["status"], "error")
        self.assertIn("no repository configured", result["output"])

    def test_readfile_returns_file_contents(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "mod.py").write_text("def f(): return 42")
            with mock.patch.dict(os.environ, {tools.REPO_ROOT_ENV: d}):
                result = tools.run_readfile("mod.py")
        self.assertEqual(result["status"], "ok")
        self.assertIn("return 42", result["output"])

    def test_readfile_prefers_a_lab_specific_repository(self):
        with tempfile.TemporaryDirectory() as global_dir, tempfile.TemporaryDirectory() as lab_dir:
            (Path(global_dir) / "which.txt").write_text("global")
            (Path(lab_dir) / "which.txt").write_text("lab nine")
            env = {
                tools.REPO_ROOT_ENV: global_dir,
                f"{tools.REPO_ROOT_ENV}_9": lab_dir,
            }
            with mock.patch.dict(os.environ, env, clear=True):
                result = tools.run_readfile("which.txt", lab_id=9)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["output"], "lab nine")

    def test_readfile_refuses_paths_outside_the_root(self):
        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer) / "repo"
            root.mkdir()
            (Path(outer) / "secret.txt").write_text("nope")
            with mock.patch.dict(os.environ, {tools.REPO_ROOT_ENV: str(root)}):
                result = tools.run_readfile("../secret.txt")
        self.assertEqual(result["status"], "error")
        self.assertIn("outside the repository", result["output"])

    def test_readfile_refuses_symlinks_leaving_the_root(self):
        with tempfile.TemporaryDirectory() as outer:
            root = Path(outer) / "repo"
            root.mkdir()
            secret = Path(outer) / "secret.txt"
            secret.write_text("nope")
            try:
                (root / "link.txt").symlink_to(secret)
            except OSError:
                self.skipTest("symlinks unavailable")
            with mock.patch.dict(os.environ, {tools.REPO_ROOT_ENV: str(root)}):
                result = tools.run_readfile("link.txt")
        self.assertEqual(result["status"], "error")

    def test_propose_patch_records_without_applying(self):
        """The whole point: the patch is an artifact, not a mutation."""
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
        result = tools.run_propose_patch(diff)
        self.assertEqual(result["status"], "ok")
        self.assertIn("has not been applied", result["output"])
        self.assertIn("nothing you write here can change the running system",
                      result["output"])

    def test_propose_patch_flags_non_diffs(self):
        result = tools.run_propose_patch("please make the daemon faster")
        self.assertIn("does not look like a unified diff", result["output"])

    def test_empty_patch_is_refused(self):
        self.assertEqual(tools.run_propose_patch("   ")["status"], "error")

    def test_patch_artifact_stores_the_diff_itself(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-old\n+new\n"
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            tools.execute_tool_calls(
                conn, [("propose_patch", diff)],
                lab_id=ids["lab_id"], task_id=ids["task_id"],
                student_id=ids["student_id"], lab_dir=lab_dir,
            )
            row = conn.execute("SELECT * FROM tool_runs").fetchone()
            self.assertEqual(row["tool"], "propose_patch")
            self.assertTrue(row["output_path"].endswith(".patch"))
            # The stored artifact must be the diff a human can apply,
            # not the acknowledgement message.
            self.assertIn("+new", (lab_dir / row["output_path"]).read_text())
        conn.close()


class ApplyPatchTests(unittest.TestCase):
    """apply_patch really changes the repo -- so its guards are the tests
    that matter most."""

    def _repo(self, tmp):
        root = Path(tmp) / "repo"
        root.mkdir()
        for args in (["init", "-q"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
            subprocess.run(["git", *args], cwd=root, capture_output=True)
        (root / "x.py").write_text("VALUE = 1\n")
        (root / "run_tests.sh").write_text("#!/bin/sh\ngrep -q 'VALUE = 2' x.py\n")
        (root / "run_tests.sh").chmod(0o755)
        subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=root, capture_output=True)
        return root

    _GOOD = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
    _BAD = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 99\n"

    def test_passing_patch_is_committed_on_the_labs_branch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            with mock.patch.dict(os.environ, {tools.REPO_ROOT_ENV: str(root)}):
                result = tools.run_apply_patch(self._GOOD)
            self.assertEqual(result["status"], "ok", result["output"])
            self.assertIn("committed", result["output"])
            self.assertEqual((root / "x.py").read_text().strip(), "VALUE = 2")
            branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                    cwd=root, capture_output=True, text=True).stdout.strip()
            self.assertEqual(branch, "auto-research")

    def test_lab_specific_patch_uses_the_lab_workspace_and_branch(self):
        with tempfile.TemporaryDirectory() as global_tmp, tempfile.TemporaryDirectory() as lab_tmp:
            global_root = self._repo(global_tmp)
            lab_root = self._repo(lab_tmp)
            env = {
                tools.REPO_ROOT_ENV: str(global_root),
                f"{tools.REPO_ROOT_ENV}_9": str(lab_root),
            }
            with mock.patch.dict(os.environ, env, clear=True):
                result = tools.run_apply_patch(self._GOOD, lab_id=9)
            self.assertEqual(result["status"], "ok", result["output"])
            self.assertEqual((lab_root / "x.py").read_text().strip(), "VALUE = 2")
            self.assertEqual((global_root / "x.py").read_text().strip(), "VALUE = 1")
            branch = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=lab_root,
                capture_output=True, text=True,
            ).stdout.strip()
            self.assertEqual(branch, "auto-research-lab-9")

    def test_failing_patch_is_reverted_and_leaves_no_trace(self):
        """This is what makes self-modification survivable."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            with mock.patch.dict(os.environ, {tools.REPO_ROOT_ENV: str(root)}):
                result = tools.run_apply_patch(self._BAD)
            self.assertEqual(result["status"], "error")
            self.assertIn("REVERTED", result["output"])
            self.assertEqual((root / "x.py").read_text().strip(), "VALUE = 1")
            self.assertEqual(
                subprocess.run(["git", "status", "--porcelain"], cwd=root,
                               capture_output=True, text=True).stdout.strip(), "")

    def test_refuses_a_dirty_tree(self):
        """A human's uncommitted work must never be swept into the lab's
        commit, nor destroyed by the revert path."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            (root / "wip.py").write_text("my unsaved work")
            with mock.patch.dict(os.environ, {tools.REPO_ROOT_ENV: str(root)}):
                result = tools.run_apply_patch(self._GOOD)
            self.assertEqual(result["status"], "error")
            self.assertIn("uncommitted change", result["output"])
            self.assertEqual((root / "wip.py").read_text(), "my unsaved work")

    def test_malformed_patch_is_rejected_before_touching_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            with mock.patch.dict(os.environ, {tools.REPO_ROOT_ENV: str(root)}):
                result = tools.run_apply_patch("not a diff at all")
            self.assertEqual(result["status"], "error")
            self.assertEqual((root / "x.py").read_text().strip(), "VALUE = 1")

    _NEW_FILE_FAILING = (
        "diff --git a/added.py b/added.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/added.py\n"
        "@@ -0,0 +1 @@\n"
        "+ADDED = True\n"
    )

    def test_failing_patch_that_added_a_file_does_not_deadlock_the_tool(self):
        """checkout only restores tracked files, so a reverted new file used
        to linger as untracked and trip the dirty-tree guard forever after."""
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            with mock.patch.dict(os.environ, {tools.REPO_ROOT_ENV: str(root)}):
                first = tools.run_apply_patch(self._NEW_FILE_FAILING)
                self.assertEqual(first["status"], "error")
                self.assertIn("REVERTED", first["output"])
                self.assertFalse((root / "added.py").exists())
                self.assertEqual(
                    subprocess.run(["git", "status", "--porcelain"], cwd=root,
                                   capture_output=True, text=True).stdout.strip(), "")
                # The next patch must still be accepted, not refused as dirty.
                second = tools.run_apply_patch(self._GOOD)
            self.assertEqual(second["status"], "ok", second["output"])

    def test_wrong_hunk_counts_are_recounted_rather_than_rejected(self):
        """The commonest model diff defect: correct edit, wrong @@ arithmetic."""
        miscounted = "--- a/x.py\n+++ b/x.py\n@@ -1,9 +1,9 @@\n-VALUE = 1\n+VALUE = 2\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            self.assertNotEqual(
                subprocess.run(["git", "apply", "--check", "-"], cwd=root, input=miscounted,
                               capture_output=True, text=True).returncode, 0,
                "fixture must be one plain git apply rejects")
            with mock.patch.dict(os.environ, {tools.REPO_ROOT_ENV: str(root)}):
                result = tools.run_apply_patch(miscounted)
            self.assertEqual(result["status"], "ok", result["output"])
            self.assertEqual((root / "x.py").read_text().strip(), "VALUE = 2")

    def test_truncated_patch_is_reported_as_malformed_not_out_of_date(self):
        truncated = (
            "diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
            "@@ -1 +1 @@\n-VALUE = 1\n+VALUE = 2\n"
            "@@ -17,10 +19,14 @@ def some"
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            with mock.patch.dict(os.environ, {tools.REPO_ROOT_ENV: str(root)}):
                result = tools.run_apply_patch(truncated)
            self.assertEqual(result["status"], "error")
            self.assertIn("truncated", result["output"])
            self.assertEqual((root / "x.py").read_text().strip(), "VALUE = 1")

    def test_needs_a_configured_repository(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(tools.run_apply_patch("--- a\n+++ b\n")["status"], "error")


class FetchTests(unittest.TestCase):
    """Internet access is allowlisted, not open: an agent that can reach
    anything can be steered by whatever it reads."""

    def test_open_without_an_allowlist(self):
        # Default-open: an unset allowlist means any host, so the request
        # gets as far as the network rather than being refused by policy.
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("urllib.request.urlopen", side_effect=OSError("boom")):
                result = tools.run_fetch("https://example.com")
        self.assertNotIn("not on this lab's allowlist", result["output"])
        self.assertNotIn("no internet access", result["output"])

    def test_star_allowlist_means_any_host(self):
        with mock.patch.dict(os.environ, {tools.FETCH_ALLOW_ENV: "*"}, clear=True):
            self.assertIsNone(tools._fetch_allowlist(None))

    def test_host_not_on_the_allowlist_is_refused(self):
        with mock.patch.dict(os.environ, {tools.FETCH_ALLOW_ENV: "data.gov,example.com"}):
            result = tools.run_fetch("https://elsewhere.test/x")
        self.assertEqual(result["status"], "error")
        self.assertIn("not on this lab's allowlist", result["output"])

    def test_subdomains_of_an_allowed_host_are_permitted(self):
        with mock.patch.dict(os.environ, {tools.FETCH_ALLOW_ENV: "example.com"}):
            refused = tools.run_fetch("https://notexample.com/x")
        self.assertIn("not on this lab", refused["output"])

    def test_non_http_schemes_are_refused(self):
        with mock.patch.dict(os.environ, {tools.FETCH_ALLOW_ENV: "example.com"}):
            for url in ("file:///etc/passwd", "ftp://example.com/x"):
                self.assertEqual(tools.run_fetch(url)["status"], "error")

    def test_empty_url_is_refused(self):
        with mock.patch.dict(os.environ, {tools.FETCH_ALLOW_ENV: "example.com"}):
            self.assertEqual(tools.run_fetch("   ")["status"], "error")


class VerifyIsolationTests(unittest.TestCase):
    def test_verification_is_network_isolated(self):
        """A verification that can reach the internet is not reproducible.
        The docs promised this before the code did."""
        if not tools._network_isolation_available():
            self.skipTest("user namespaces unavailable on this host")
        result = tools.run_verifier(
            "import urllib.request\n"
            "try:\n"
            "    urllib.request.urlopen('https://example.com', timeout=5)\n"
            "    print('REACHABLE')\n"
            "except Exception as e:\n"
            "    print('offline:', type(e).__name__)\n",
            timeout=25,
        )
        self.assertNotIn("REACHABLE", result["output"])

    def test_ordinary_computation_still_works_under_isolation(self):
        result = tools.run_verifier("print('RESULT:', sum(range(100)))")
        self.assertEqual(result["status"], "ok")
        self.assertIn("4950", result["output"])


class ExperimentTests(unittest.TestCase):
    """Only a lab whose subject is this system may spawn research runs."""

    def test_enabled_by_default(self):
        # Default-open: refusal, if any, must come from missing wiring
        # (AUTOPROF_DB_PATH) and not from the lab gate.
        with mock.patch.dict(os.environ, {}, clear=True):
            result = tools.run_experiment('{"idea": "x"}', lab_id=2)
        self.assertNotIn("switched off", result["output"])

    def test_switched_off_when_the_gate_names_other_labs(self):
        with mock.patch.dict(os.environ, {tools.EXPERIMENT_LABS_ENV: "7"}, clear=True):
            result = tools.run_experiment('{"idea": "x"}', lab_id=2)
        self.assertEqual(result["status"], "error")
        self.assertIn("switched off", result["output"])

    def test_only_allowlisted_labs(self):
        with mock.patch.dict(os.environ, {tools.EXPERIMENT_LABS_ENV: "2"}):
            self.assertEqual(tools.run_experiment('{"idea": "x"}', lab_id=1)["status"], "error")

    def test_spec_must_be_json_with_an_idea_or_measure(self):
        with mock.patch.dict(os.environ, {tools.EXPERIMENT_LABS_ENV: "2"}):
            self.assertEqual(tools.run_experiment("not json", lab_id=2)["status"], "error")
            r = tools.run_experiment("{}", lab_id=2)
            self.assertIn("needs an 'idea'", r["output"])

    def test_production_paths_are_required(self):
        env = {tools.EXPERIMENT_LABS_ENV: "2"}
        with mock.patch.dict(os.environ, env, clear=True):
            result = tools.run_experiment('{"idea": "x"}', lab_id=2)
        self.assertEqual(result["status"], "error")
        self.assertIn("AUTOPROF_DB_PATH", result["output"])

    def test_workspace_experiment_runs_only_for_the_scoped_lab(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "experiments").mkdir()
            (root / "experiments" / "ok.py").write_text("print('REAL EXPERIMENT')\n")
            env = {
                f"{tools.REPO_ROOT_ENV}_9": d,
                f"{tools.WORKSPACE_EXEC_LABS_ENV}_9": "9",
            }
            body = json.dumps({"command": ["python3", "experiments/ok.py"]})
            with mock.patch.dict(os.environ, env, clear=True):
                denied = tools.run_experiment(body, lab_id=8)
                result = tools.run_experiment(body, lab_id=9)
        self.assertEqual(denied["status"], "error")
        self.assertEqual(result["status"], "ok")
        self.assertIn("REAL EXPERIMENT", result["output"])

    def test_workspace_experiment_refuses_shell_and_path_escape(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "experiments").mkdir()
            env = {
                f"{tools.REPO_ROOT_ENV}_9": d,
                f"{tools.WORKSPACE_EXEC_LABS_ENV}_9": "9",
            }
            with mock.patch.dict(os.environ, env, clear=True):
                shell = tools.run_experiment(
                    json.dumps({"command": ["sh", "-c", "echo nope"]}), lab_id=9,
                )
                escaped = tools.run_experiment(
                    json.dumps({"command": ["python3", "../outside.py"]}), lab_id=9,
                )
        self.assertEqual(shell["status"], "error")
        self.assertEqual(escaped["status"], "error")

    def test_measure_reports_only_that_lab(self):
        """An experiment's numbers must never be contaminated by the other
        labs sharing the database."""
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            from autoprof import db as _db
            path = Path(d) / "x.db"
            conn = _db.connect(path)
            _db.ensure_initialized(conn)
            for name in ("A", "B"):
                cur = conn.execute(
                    "INSERT INTO professors (lab_id,name,field,status,memory_path) "
                    "VALUES (NULL,?,'f','active','m')", (name,))
                pid = cur.lastrowid
                cur = conn.execute(
                    "INSERT INTO labs (professor_id,root_problem,status) VALUES (?,?,'active')",
                    (pid, name))
                lab_id = cur.lastrowid
                conn.execute("UPDATE professors SET lab_id=? WHERE id=?", (lab_id, pid))
                conn.execute(
                    "INSERT INTO tasks (lab_id,title,brief_path,direction,end_criteria,status) "
                    "VALUES (?,?,'b','prove','e','open')", (lab_id, name))
            conn.commit()
            conn.close()

            out = tools._measure(str(path), 1)
            self.assertIn("LAB #1", out)
            self.assertIn("tasks: open=1", out)   # not 2 -- lab 2's task excluded


if __name__ == "__main__":
    unittest.main()


class RecordToolTests(unittest.TestCase):
    """The meta-lab's evidence is what this installation DID, not what its
    source says it should do."""

    def _db(self):
        import sqlite3
        from autoprof import db as db_module
        path = Path(tempfile.mkdtemp()) / "rec.db"
        conn = db_module.connect(path)
        db_module.ensure_initialized(conn)
        cur = conn.execute(
            "INSERT INTO professors (lab_id, name, field, status, memory_path) "
            "VALUES (NULL, 'P', 'F', 'active', 'm.md')"
        )
        conn.execute(
            "INSERT INTO labs (professor_id, root_problem, status) VALUES (?, 'rp', 'active')",
            (cur.lastrowid,),
        )
        conn.commit()
        conn.close()
        return str(path)

    def test_returns_rows_for_a_known_slice(self):
        out = tools.run_record("labs", db_path=self._db())
        self.assertEqual(out["status"], "ok")
        self.assertIn("root_problem", out["output"])

    def test_an_unknown_slice_lists_the_menu(self):
        out = tools.run_record("whatever", db_path=self._db())
        self.assertEqual(out["status"], "error")
        self.assertIn("verdicts", out["output"])

    def test_free_form_sql_is_not_accepted(self):
        """A student that can write its own query can write the one that
        supports the claim it already made."""
        out = tools.run_record("SELECT * FROM labs", db_path=self._db())
        self.assertEqual(out["status"], "error")

    def test_the_record_cannot_be_written_to(self):
        path = self._db()
        tools.RECORD_QUERIES["_probe"] = ("t", "DELETE FROM labs")
        try:
            out = tools.run_record("_probe", db_path=path)
        finally:
            del tools.RECORD_QUERIES["_probe"]
        self.assertEqual(out["status"], "error")
        self.assertIn("readonly", out["output"].lower())

    def test_missing_database_is_reported_not_raised(self):
        import os
        saved = os.environ.pop("AUTOPROF_DB_PATH", None)
        try:
            out = tools.run_record("labs")
            self.assertEqual(out["status"], "error")
        finally:
            if saved:
                os.environ["AUTOPROF_DB_PATH"] = saved

    def test_the_tool_block_is_recognised(self):
        calls = tools.parse_tool_calls("```tool:record\nverdicts\n```")
        self.assertEqual(calls, [("record", "verdicts\n")])

    def test_docs_tell_the_student_the_record_beats_the_design(self):
        self.assertIn("the record wins", tools.TOOL_DOCS)


class VerifierIsolationTests(unittest.TestCase):
    """A verify program wrote three papers and eighteen reviews into the
    production database -- the operational record its own lab existed to
    measure. `-I` was documented as preventing exactly that and does not:
    it governs module resolution, not what sqlite3 may open."""

    def test_readonly_paths_block_writes_but_not_computation(self):
        import sqlite3

        if not tools._network_isolation_available():
            self.skipTest("namespaces unavailable in this environment")
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "record.db"
            sqlite3.connect(target).execute("CREATE TABLE t (x)")
            code = (
                "import sqlite3\n"
                f"try:\n"
                f"    c = sqlite3.connect({str(target)!r})\n"
                "    c.execute('INSERT INTO t VALUES (1)'); c.commit()\n"
                "    print('WROTE')\n"
                "except Exception as e:\n"
                "    print('blocked')\n"
                "print('sum', 2 + 2)\n"
            )
            result = tools.run_verifier(code, readonly_paths=(d,))

        self.assertEqual(result["status"], "ok")
        self.assertIn("blocked", result["output"])
        self.assertNotIn("WROTE", result["output"])
        # the point of the tool still works
        self.assertIn("sum 4", result["output"])

    def test_record_finds_the_db_through_the_connection(self):
        # It used to look this up in AUTOPROF_DB_PATH, which nothing set,
        # so the meta lab's evidence tool failed 62 times out of 62 while
        # the caller held an open connection to the very database.
        from autoprof import db as _db

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rec.db"
            conn = _db.connect(path)
            _db.ensure_initialized(conn)
            self.assertEqual(tools._conn_db_path(conn), str(path))
            self.assertEqual(
                tools.run_record("labs", db_path=tools._conn_db_path(conn))["status"], "ok"
            )
            conn.close()


class TaskHomeShellTests(unittest.TestCase):
    """`shell` gives one task a private, persistent home with a real shell."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lab_dir = Path(self.tmp.name)
        patcher = mock.patch.dict(
            os.environ, {tools.TASK_HOME_LABS_ENV + "_9": "9"}, clear=False
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, body, lab_id=9, task_id=34):
        return tools.run_shell(body, lab_id=lab_id, task_id=task_id, lab_dir=self.lab_dir)

    def test_open_by_default_for_an_unnamed_lab(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            result = self._run("echo hi", lab_id=7)
        self.assertEqual(result["status"], "ok")

    def test_switched_off_when_the_gate_names_other_labs(self):
        with mock.patch.dict(os.environ, {tools.TASK_HOME_LABS_ENV: "9"}, clear=True):
            result = self._run("echo hi", lab_id=7)
        self.assertEqual(result["status"], "error")
        self.assertIn(tools.TASK_HOME_LABS_ENV, result["output"])

    def test_runs_a_script_in_the_task_home(self):
        result = self._run("pwd && echo marker")
        self.assertEqual(result["status"], "ok")
        self.assertIn("marker", result["output"])
        self.assertIn("tasks/34/home", result["output"])

    def test_filesystem_persists_between_calls(self):
        self.assertEqual(self._run("mkdir -p sub && echo kept > sub/f")["status"], "ok")
        second = self._run("cat sub/f")
        self.assertEqual(second["status"], "ok")
        self.assertIn("kept", second["output"])

    def test_nonzero_exit_is_an_error_with_output(self):
        result = self._run("echo before; exit 3")
        self.assertEqual(result["status"], "error")
        self.assertIn("exit=3", result["output"])
        self.assertIn("before", result["output"])

    def test_artifacts_are_listed_back(self):
        result = self._run("printf 12345 > artifacts/result.json")
        self.assertIn("artifacts/result.json", result["output"])
        self.assertIn("5 bytes", result["output"])

    def test_home_is_redirected_into_the_task_tree(self):
        # Otherwise git and docker write per-user state to the daemon's own
        # home, which may be read-only to the research process.
        result = self._run("echo $HOME; echo $TMPDIR")
        self.assertEqual(result["status"], "ok")
        self.assertIn("tasks/34/home", result["output"])
        self.assertIn("tasks/34/home/tmp", result["output"])

    def test_daemon_secrets_are_not_visible(self):
        with mock.patch.dict(
            os.environ,
            {"AUTOPROF_API_TOKEN": "leak-me", "SOME_API_KEY": "leak-too"},
            clear=False,
        ):
            result = self._run("env")
        self.assertNotIn("leak-me", result["output"])
        self.assertNotIn("leak-too", result["output"])

    def test_json_form_with_timeout_is_accepted(self):
        result = self._run('{"script": "echo json-form", "timeout": 30}')
        self.assertEqual(result["status"], "ok")
        self.assertIn("json-form", result["output"])

    def test_timeout_is_reported_as_timeout(self):
        result = self._run('{"script": "sleep 5", "timeout": 1}')
        self.assertEqual(result["status"], "timeout")

    def test_empty_script_is_refused(self):
        self.assertEqual(self._run("   ")["status"], "error")

    def test_shell_is_a_parseable_tool_name(self):
        calls = tools.parse_tool_calls("```tool:shell\necho hi\n```")
        self.assertEqual(calls, [("shell", "echo hi\n")])

    def test_documented(self):
        self.assertIn("**shell**", tools.render_tool_docs())


class CommitWorkspaceTests(unittest.TestCase):
    """Agentic edits must end up versioned, like apply_patch's do."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for args in (("init", "-q"), ("config", "user.email", "t@t"),
                     ("config", "user.name", "t")):
            subprocess.run(["git", *args], cwd=self.root, check=True,
                           capture_output=True)
        (self.root / "run_tests.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.root / "run_tests.sh").chmod(0o755)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "base"], cwd=self.root, check=True,
                       capture_output=True)
        self.env = mock.patch.dict(
            os.environ, {tools.REPO_ROOT_ENV + "_9": str(self.root)}, clear=False
        )
        self.env.start()
        self.addCleanup(self.env.stop)

    def _log(self):
        return subprocess.run(["git", "log", "--oneline"], cwd=self.root,
                              capture_output=True, text=True).stdout

    def test_clean_tree_is_a_noop(self):
        self.assertEqual(tools.commit_workspace(9, "m")["status"], "clean")

    def test_changes_are_committed(self):
        (self.root / "new.py").write_text("x = 1\n")
        result = tools.commit_workspace(9, "student work: round 5")
        self.assertEqual(result["status"], "ok")
        self.assertIn("student work: round 5", self._log())

    def test_untracked_files_are_included(self):
        (self.root / "artifacts").mkdir()
        (self.root / "artifacts" / "r.json").write_text("{}")
        tools.commit_workspace(9, "m")
        tracked = subprocess.run(["git", "ls-files"], cwd=self.root,
                                 capture_output=True, text=True).stdout
        self.assertIn("artifacts/r.json", tracked)

    def test_test_outcome_is_recorded(self):
        (self.root / "new.py").write_text("x = 1\n")
        self.assertTrue(tools.commit_workspace(9, "m")["tests_passed"])

    def test_failing_tests_still_commit_but_are_flagged(self):
        # Reverting here would destroy integrated research, unlike
        # apply_patch which owns one small diff.
        (self.root / "run_tests.sh").write_text("#!/bin/sh\nexit 1\n")
        (self.root / "new.py").write_text("x = 1\n")
        result = tools.commit_workspace(9, "m")
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["tests_passed"])
        body = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=self.root,
                              capture_output=True, text=True).stdout
        self.assertIn("TESTS FAILED", body)
        self.assertTrue((self.root / "new.py").exists())

    def test_lab_without_workspace_is_skipped(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(tools.commit_workspace(9, "m")["status"], "skipped")


class ToolCallCapTests(unittest.TestCase):
    """Extra calls must be countable, not silently vanish."""

    def _text(self, n):
        return "\n".join(f"```tool:verify\nprint({i})\n```" for i in range(n))

    def test_default_cap_is_eight(self):
        self.assertEqual(len(tools.parse_tool_calls(self._text(12))), 8)

    def test_explicit_limit_is_honoured(self):
        self.assertEqual(len(tools.parse_tool_calls(self._text(12), limit=3)), 3)

    def test_count_reports_the_pre_cap_total(self):
        self.assertEqual(tools.count_tool_calls(self._text(12)), 12)

    def test_count_matches_when_under_the_cap(self):
        self.assertEqual(tools.count_tool_calls(self._text(2)), 2)

    def test_a_zero_or_negative_limit_still_runs_one(self):
        self.assertEqual(len(tools.parse_tool_calls(self._text(5), limit=0)), 1)


class RecordArgumentFormTests(unittest.TestCase):
    """A slice name wrapped in JSON is unambiguous; accept it."""

    def _db(self):
        import tempfile
        from autoprof import db as db_module
        d = tempfile.mkdtemp()
        path = Path(d) / "r.db"
        conn = db_module.connect(path)
        db_module.ensure_initialized(conn)
        conn.close()
        return str(path)

    def test_bare_name_still_works(self):
        r = tools.run_record("labs", db_path=self._db())
        self.assertEqual(r["status"], "ok")

    def test_json_slice_form_is_accepted(self):
        r = tools.run_record('{"slice": "labs"}', db_path=self._db())
        self.assertEqual(r["status"], "ok")
        self.assertNotIn("unknown slice", r["output"])

    def test_other_obvious_keys_are_accepted(self):
        for key in ("name", "query", "record"):
            r = tools.run_record('{"%s": "jobs"}' % key, db_path=self._db())
            self.assertEqual(r["status"], "ok", key)

    def test_a_genuinely_unknown_slice_still_errors(self):
        r = tools.run_record('{"slice": "nonsense"}', db_path=self._db())
        self.assertEqual(r["status"], "error")
        self.assertIn("unknown slice", r["output"])

    def test_malformed_json_is_not_silently_accepted(self):
        r = tools.run_record('{"slice": TRUNCATED', db_path=self._db())
        self.assertEqual(r["status"], "error")


class UnopenedToolFenceTests(unittest.TestCase):
    """minimax-m3 emits the closing fence but not the opening one."""

    def test_unopened_block_is_executed(self):
        text = 'tool:shell\ncd task37 && python3 -m run_validation\n```'
        self.assertEqual(
            tools.parse_tool_calls(text),
            [("shell", "cd task37 && python3 -m run_validation\n")],
        )

    def test_call_after_prose_is_found(self):
        text = "Here is my plan.\n\ntool:readfile\ndocs/x.md\n```"
        self.assertEqual(tools.parse_tool_calls(text), [("readfile", "docs/x.md\n")])

    def test_prose_mentioning_a_tool_is_not_executed(self):
        self.assertEqual(tools.parse_tool_calls("I would use tool:shell here."), [])

    def test_missing_closing_fence_is_not_executed(self):
        self.assertEqual(tools.parse_tool_calls("tool:shell\nrm -rf /"), [])

    def test_unknown_tool_name_is_not_executed(self):
        self.assertEqual(tools.parse_tool_calls("tool:banana\necho hi\n```"), [])

    def test_properly_fenced_blocks_are_not_double_counted(self):
        text = "```tool:shell\necho a\n```\n\n```tool:shell\necho b\n```"
        self.assertEqual(tools.count_tool_calls(text), 2)

    def test_a_tool_name_inside_a_fenced_body_is_not_a_second_call(self):
        text = '```tool:shell\ngrep -n "tool:shell" x.py\n```'
        self.assertEqual(tools.count_tool_calls(text), 1)


class BinaryOutputToleranceTests(unittest.TestCase):
    """A stray byte in tool output must not take down the whole job."""

    def test_shell_survives_invalid_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            lab_dir = Path(tmp)
            out = tools.run_shell(
                r"printf 'before\xa7after\n'",
                lab_id=1, task_id=1, lab_dir=lab_dir,
            )
            self.assertEqual(out["status"], "ok")
            self.assertIn("before", out["output"])
            self.assertIn("after", out["output"])


class GitBinaryOutputTests(unittest.TestCase):
    """git prints byte-for-byte filenames; a non-UTF-8 one crashed the job."""

    def test_git_helper_survives_a_non_utf8_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tools._git(root, "init", "-q")
            (root / b"odd-\xa7-name.txt".decode("latin-1")).write_bytes(b"x")
            done = tools._git(root, "status", "--porcelain")
            self.assertEqual(done.returncode, 0)
            self.assertIn("odd-", done.stdout)


class FenceInsideToolBodyTests(unittest.TestCase):
    """A shell script that writes markdown has fences inside its own body."""

    def test_heredoc_containing_a_fence_survives_intact(self):
        text = (
            "```tool:shell\n"
            "cat > docs/REPAIR_LOG.md <<'EOF'\n"
            "## Item 6\n"
            "```\n"
            "grep -rn '0x4588' .\n"
            "```\n"
            "EOF\n"
            "wc -c docs/REPAIR_LOG.md\n"
            "```\n"
        )
        (name, body), = tools.parse_tool_calls(text)
        self.assertEqual(name, "shell")
        self.assertIn("EOF", body.split("wc -c")[0])
        self.assertIn("wc -c docs/REPAIR_LOG.md", body)

    def test_two_calls_do_not_merge(self):
        text = "```tool:shell\necho a\n```\n\n```tool:readfile\nx.md\n```\n"
        self.assertEqual(
            [name for name, _ in tools.parse_tool_calls(text)], ["shell", "readfile"]
        )

    def test_an_unterminated_block_is_not_executed(self):
        self.assertEqual(tools.parse_tool_calls("```tool:shell\nrm -rf /\n"), [])


class InlineFenceOpenerTests(unittest.TestCase):
    """Models run the opener straight on from prose, with no newline."""

    def test_opener_immediately_after_prose_is_recognised(self):
        text = "Let me locate the tree.```tool:shell\ncd /x && ls\n```"
        self.assertEqual(tools.parse_tool_calls(text), [("shell", "cd /x && ls\n")])

    def test_inline_opener_does_not_trip_the_unparsed_guard(self):
        text = "First locate the template.```tool:shell\ncd /x && pwd\n```"
        self.assertFalse(tools.has_unparsed_tool_syntax(text))

    def test_inline_opener_still_ends_at_the_last_fence(self):
        text = "Do it.```tool:shell\ncat <<'EOF'\n```\nx\n```\nEOF\ndone\n```"
        (_, body), = tools.parse_tool_calls(text)
        self.assertIn("EOF\ndone", body)
