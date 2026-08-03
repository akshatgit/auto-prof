import tempfile
from pathlib import Path
import http.client
import threading
from pathlib import Path
import unittest

from autoprof import webserver
from tests.helpers import fresh_db, seed_lab_with_student


class RenderLabListTests(unittest.TestCase):
    def test_lists_labs_with_status_and_problem(self):
        conn = fresh_db()
        seed_lab_with_student(conn)
        html = webserver.render_lab_list(conn)
        self.assertIn("test problem", html)
        self.assertIn("active", html)
        conn.close()

    def test_escapes_html_in_root_problem(self):
        conn = fresh_db()
        conn.execute(
            "INSERT INTO professors (lab_id, name, field, status, memory_path) "
            "VALUES (NULL, 'P', 'F', 'active', 'm.md')"
        )
        pid = conn.execute("SELECT id FROM professors").fetchone()["id"]
        conn.execute(
            "INSERT INTO labs (professor_id, root_problem, status) VALUES (?, ?, 'active')",
            (pid, "<script>alert(1)</script>"),
        )
        conn.commit()
        html = webserver.render_lab_list(conn)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)
        conn.close()


class RenderLabDetailTests(unittest.TestCase):
    def test_shows_tasks_and_professor(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        html = webserver.render_lab_detail(conn, ids["lab_id"])
        self.assertIsNotNone(html)
        self.assertIn("Task 1", html)
        self.assertIn("Prof Test", html)
        conn.close()

    def test_missing_lab_returns_none(self):
        conn = fresh_db()
        self.assertIsNone(webserver.render_lab_detail(conn, 999))
        conn.close()

    def test_shows_reviews_if_any(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        conn.execute(
            "INSERT INTO reviews (target_type, target_id, review_round, reviewer_index, verdict, rationale_path) "
            "VALUES ('lab', ?, 1, 1, 'strong_accept', 'r.md')",
            (ids["lab_id"],),
        )
        conn.commit()
        html = webserver.render_lab_detail(conn, ids["lab_id"])
        self.assertIn("strong_accept", html)
        conn.close()


class RenderStudentDetailTests(unittest.TestCase):
    def test_shows_status_and_task(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        html = webserver.render_student_detail(conn, ids["student_id"])
        self.assertIsNotNone(html)
        self.assertIn("working", html)
        conn.close()

    def test_missing_student_returns_none(self):
        conn = fresh_db()
        self.assertIsNone(webserver.render_student_detail(conn, 999))
        conn.close()


class RenderProfessorDetailTests(unittest.TestCase):
    def test_shows_field_and_students(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        html = webserver.render_professor_detail(conn, ids["professor_id"])
        self.assertIsNotNone(html)
        self.assertIn("Testing", html)
        conn.close()

    def test_missing_professor_returns_none(self):
        conn = fresh_db()
        self.assertIsNone(webserver.render_professor_detail(conn, 999))
        conn.close()


class LiveServerTests(unittest.TestCase):
    """A couple of genuine socket-level checks -- routing wiring, 404s --
    on top of the pure render_* unit tests above."""

    def setUp(self):
        import tempfile
        self._tmpdir = tempfile.TemporaryDirectory()
        db_path = f"{self._tmpdir.name}/test.db"
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        from tests.helpers import SCHEMA_PATH
        conn.executescript(SCHEMA_PATH.read_text())
        conn.row_factory = sqlite3.Row
        self.ids = seed_lab_with_student(conn)
        conn.close()

        self.server = webserver.make_server(db_path, host="127.0.0.1", port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self._tmpdir.cleanup()

    def _get(self, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        return resp.status, body

    def test_index_lists_labs(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn("test problem", body)

    def test_lab_detail(self):
        status, body = self._get(f"/labs/{self.ids['lab_id']}")
        self.assertEqual(status, 200)
        self.assertIn("Task 1", body)

    def test_unknown_lab_is_404(self):
        status, body = self._get("/labs/999999")
        self.assertEqual(status, 404)

    def test_unknown_route_is_404(self):
        status, body = self._get("/nope")
        self.assertEqual(status, 404)


class PaperMathRenderingTests(unittest.TestCase):
    """Papers are mathematics; without MathJax they display as literal
    \\( ... \\) source."""

    def test_mathjax_injected_into_a_paper_lacking_it(self):
        doc = "<html><head><title>P</title></head><body><h1>P</h1>\\(x\\)</body></html>"
        out = webserver._ensure_mathjax(doc)
        self.assertIn("mathjax", out.lower())
        self.assertIn("<h1>P</h1>", out)

    def test_injection_is_idempotent(self):
        doc = "<html><head><title>P</title></head><body>\\(x\\)</body></html>"
        once = webserver._ensure_mathjax(doc)
        self.assertEqual(webserver._ensure_mathjax(once), once)

    def test_paper_already_carrying_mathjax_is_untouched(self):
        doc = "<html><head><script src='x/mathjax@3/y.js'></script></head><body></body></html>"
        self.assertEqual(webserver._ensure_mathjax(doc), doc)

    def test_document_with_no_head_still_gets_mathjax(self):
        doc = "<h1>bare fragment</h1>\\(x\\)"
        self.assertIn("mathjax", webserver._ensure_mathjax(doc).lower())


class PlainPreviewTests(unittest.TestCase):
    def test_latex_is_stripped_for_list_previews(self):
        raw = r"Let \(( E,\mathcal I )\) be a system with \[\frac{a}{b}\]"
        out = webserver._plain_preview(raw)
        self.assertNotIn("\\mathcal", out)
        self.assertNotIn("{", out)
        self.assertIn("Let", out)

    def test_preview_is_truncated_with_ellipsis(self):
        out = webserver._plain_preview("word " * 100, limit=20)
        self.assertTrue(out.endswith("..."))
        self.assertLessEqual(len(out), 23)


class ArtifactPathTests(unittest.TestCase):
    def test_path_traversal_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d) / "lab"
            root.mkdir()
            (Path(d) / "secret.txt").write_text("nope")
            self.assertIsNone(webserver._read_artifact(root, "../secret.txt"))

    def test_missing_file_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(webserver._read_artifact(Path(d), "nope.html"))

    def test_reads_a_file_inside_lab_dir(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "sub").mkdir()
            (root / "sub" / "p.html").write_text("hello")
            self.assertEqual(webserver._read_artifact(root, "sub/p.html"), "hello")


class TaskTimelineTests(unittest.TestCase):
    """Eleven `continue` meetings followed by a forced write-up is a story
    no table of counts tells."""

    def test_empty_history_says_so(self):
        self.assertIn("no supervision or review history",
                      webserver.render_task_timeline([], []))

    def test_meetings_and_rounds_share_one_axis(self):
        svg = webserver.render_task_timeline(
            [{"round": 1, "verdict": "continue", "path": "p"},
             {"round": 2, "verdict": "ready", "path": "p"}],
            [{"round": 1, "verdicts": ["accept", "reject", "accept"], "strong": 0}],
        )
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("supervision", svg)
        self.assertIn("review", svg)
        self.assertIn("m1", svg)
        self.assertIn("r1", svg)

    def test_verdicts_carry_text_not_colour_alone(self):
        """Colour alone excludes colour-blind readers and greyscale."""
        svg = webserver.render_task_timeline(
            [{"round": 1, "verdict": "ready", "path": "p"}],
            [{"round": 1, "verdicts": ["strong_accept"], "strong": 1}],
        )
        self.assertIn("READY", svg)      # supervision verdict spelled out
        self.assertIn("1x++", svg)       # strong_accept count spelled out
        self.assertIn("<title>strong_accept</title>", svg)  # hover text

    def test_each_reviewer_is_drawn_separately(self):
        """A round is three verdicts; averaging them into one mark hides
        a 2-1 split."""
        svg = webserver.render_task_timeline(
            [], [{"round": 1, "verdicts": ["strong_accept", "reject", "accept"], "strong": 1}]
        )
        self.assertEqual(svg.count("<rect"), 3)

    def test_svg_scales_with_history_length(self):
        long_history = [{"round": i, "verdict": "continue", "path": "p"} for i in range(1, 12)]
        wide = webserver.render_task_timeline(long_history, [])
        narrow = webserver.render_task_timeline(long_history[:2], [])
        self.assertGreater(len(wide), len(narrow))


class TaskDetailTests(unittest.TestCase):
    def test_missing_task_is_none(self):
        conn = fresh_db()
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(webserver.render_task_detail(conn, 999, Path(d)))
        conn.close()

    def test_shows_supervision_papers_and_ledger(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        conn.execute(
            "INSERT INTO supervisions (task_id, student_id, round, verdict, guidance_path) "
            "VALUES (?, ?, 1, 'continue', 'g.md')", (ids["task_id"], ids["student_id"]))
        conn.execute(
            "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
            "VALUES (?, ?, 'p.html', 'A Paper', 'in_review', 1)",
            (ids["task_id"], ids["student_id"]))
        conn.execute(
            "INSERT INTO assumptions (lab_id, task_id, student_id, statement, source, status) "
            "VALUES (?, ?, ?, 'an inherited premise', 'brief', 'assumed')",
            (ids["lab_id"], ids["task_id"], ids["student_id"]))
        conn.commit()

        with tempfile.TemporaryDirectory() as d:
            html = webserver.render_task_detail(conn, ids["task_id"], Path(d))
        self.assertIn("Long-horizon progress", html)
        self.assertIn("A Paper", html)
        self.assertIn("an inherited premise", html)
        self.assertIn("meeting 1", html)
        conn.close()


if __name__ == "__main__":
    unittest.main()
