"""Tests for the student work -> paper draft path (autoprof/paper.py)."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import paper  # noqa: E402
from autoprof.backends.base import Backend, BackendResult  # noqa: E402
from tests.helpers import fresh_db, seed_lab_with_student  # noqa: E402

MINIMAL_PAPER = """<meta charset="utf-8">
<title>On the Hardness of Testing</title>
<header class="paper"><h1>On the Hardness of Testing</h1></header>
<main><h2>Introduction</h2><p>text</p></main>
"""


class ScriptedBackend(Backend):
    name = "scripted"

    def __init__(self, result: BackendResult):
        self.result = result
        self.calls = []

    def run(self, prompt, **opts):
        self.calls.append(prompt)
        return self.result


def _enqueue(conn, kind: str, task_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status) VALUES (?, 'task', ?, 'pending')",
        (kind, task_id),
    )
    conn.commit()
    return cur.lastrowid


class ExtractTitleTests(unittest.TestCase):
    def test_prefers_title_tag(self):
        self.assertEqual(paper.extract_title(MINIMAL_PAPER, "fallback"), "On the Hardness of Testing")

    def test_falls_back_to_h1(self):
        html = "<h1>Only an <em>H1</em> Here</h1>"
        self.assertEqual(paper.extract_title(html, "fallback"), "Only an H1 Here")

    def test_falls_back_to_task_title_when_placeholder_left_in(self):
        self.assertEqual(paper.extract_title("<title>{Title}</title>", "Task 1"), "Task 1")

    def test_falls_back_when_no_title_at_all(self):
        self.assertEqual(paper.extract_title("<p>no title</p>", "Task 1"), "Task 1")


class StudentWorkJobTests(unittest.TestCase):
    def test_writes_memory_and_enqueues_write_paper_job(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _enqueue(conn, "student_work", ids["task_id"])
        backend = ScriptedBackend(BackendResult(text="I proved the lemma.", model_version="m1"))

        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            outcome = paper.execute_student_work_job(conn, job_id, backend, lab_dir)
            self.assertEqual(outcome, "done")
            self.assertEqual(
                (lab_dir / ids["student_memory_path"]).read_text(), "I proved the lemma."
            )

        student = conn.execute(
            "SELECT * FROM students WHERE id=?", (ids["student_id"],)
        ).fetchone()
        self.assertEqual(student["status"], "writing_paper")

        followup = conn.execute(
            "SELECT * FROM jobs WHERE kind='student_write_paper'"
        ).fetchall()
        self.assertEqual(len(followup), 1)
        self.assertEqual(followup[0]["target_id"], ids["task_id"])
        conn.close()

    def test_paused_student_is_skipped_without_burning_an_attempt(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        conn.execute(
            "UPDATE students SET paused_at = datetime('now') WHERE id = ?", (ids["student_id"],)
        )
        conn.commit()
        job_id = _enqueue(conn, "student_work", ids["task_id"])
        backend = ScriptedBackend(BackendResult(text="should never run"))

        with tempfile.TemporaryDirectory() as d:
            outcome = paper.execute_student_work_job(conn, job_id, backend, Path(d))

        self.assertEqual(outcome, "not_claimed")
        self.assertEqual(backend.calls, [])
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        conn.close()

    def test_prompt_includes_task_and_memory_context(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _enqueue(conn, "student_work", ids["task_id"])
        backend = ScriptedBackend(BackendResult(text="ok"))

        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            memory_file = lab_dir / ids["student_memory_path"]
            memory_file.parent.mkdir(parents=True)
            memory_file.write_text("previously: tried induction, failed")
            paper.execute_student_work_job(conn, job_id, backend, lab_dir)

        prompt = backend.calls[0]
        self.assertIn("Task 1", prompt)
        self.assertIn("done when proved", prompt)
        self.assertIn("tried induction, failed", prompt)
        conn.close()


class StudentWritePaperJobTests(unittest.TestCase):
    def _run(self, conn, ids, lab_dir, text=MINIMAL_PAPER):
        job_id = _enqueue(conn, "student_write_paper", ids["task_id"])
        backend = ScriptedBackend(BackendResult(text=text, model_version="m1"))
        outcome = paper.execute_student_write_paper_job(conn, job_id, backend, lab_dir)
        return outcome, backend

    def test_creates_paper_row_file_and_three_review_jobs(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)

        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            outcome, _ = self._run(conn, ids, lab_dir)
            self.assertEqual(outcome, "done")

            row = conn.execute("SELECT * FROM papers").fetchone()
            self.assertEqual(row["status"], "in_review")
            self.assertEqual(row["review_round"], 1)
            self.assertEqual(row["title"], "On the Hardness of Testing")
            self.assertTrue((lab_dir / row["path"]).exists())
            self.assertIn("<h1>", (lab_dir / row["path"]).read_text())

        review_jobs = conn.execute("SELECT * FROM jobs WHERE kind='paper_review'").fetchall()
        self.assertEqual(len(review_jobs), 3)
        self.assertEqual(sorted(j["reviewer_index"] for j in review_jobs), [1, 2, 3])
        self.assertTrue(all(j["review_round"] == 1 for j in review_jobs))

        student = conn.execute(
            "SELECT * FROM students WHERE id=?", (ids["student_id"],)
        ).fetchone()
        self.assertEqual(student["status"], "in_review")
        conn.close()

    def test_strips_markdown_fence_around_html(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            self._run(conn, ids, lab_dir, text=f"```html\n{MINIMAL_PAPER}\n```")
            row = conn.execute("SELECT * FROM papers").fetchone()
            self.assertFalse((lab_dir / row["path"]).read_text().startswith("```"))
        conn.close()

    def test_non_html_output_fails_the_job_without_creating_a_paper(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            outcome, _ = self._run(conn, ids, Path(d), text="I could not write this paper.")
        self.assertIn(outcome, ("retrying", "failed"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 0)
        conn.close()

    def test_does_not_create_a_second_paper_while_one_is_in_review(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            self._run(conn, ids, lab_dir)
            outcome, backend = self._run(conn, ids, lab_dir)

        self.assertEqual(outcome, "done")
        self.assertEqual(backend.calls, [])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0], 1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='paper_review'").fetchone()[0], 3
        )
        conn.close()

    def test_prompt_carries_the_acm_template(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            _, backend = self._run(conn, ids, Path(d))
        prompt = backend.calls[0]
        self.assertIn("column-count: 2", prompt)
        self.assertIn("ACM Reference Format", prompt)
        # The template's own authoring comment is guidance for whoever
        # edits the template, not for the student writing the paper.
        self.assertNotIn("auto-prof paper template", prompt)
        conn.close()


if __name__ == "__main__":
    unittest.main()
