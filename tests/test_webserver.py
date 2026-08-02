import http.client
import threading
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


if __name__ == "__main__":
    unittest.main()
