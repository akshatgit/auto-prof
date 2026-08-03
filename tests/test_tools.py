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

    def test_needs_a_configured_repository(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(tools.run_apply_patch("--- a\n+++ b\n")["status"], "error")


class FetchTests(unittest.TestCase):
    """Internet access is allowlisted, not open: an agent that can reach
    anything can be steered by whatever it reads."""

    def test_disabled_without_an_allowlist(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            result = tools.run_fetch("https://example.com")
        self.assertEqual(result["status"], "error")
        self.assertIn("no internet access", result["output"])

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

    def test_disabled_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            result = tools.run_experiment('{"idea": "x"}', lab_id=2)
        self.assertEqual(result["status"], "error")
        self.assertIn("may not run experiments", result["output"])

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
