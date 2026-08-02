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
    def test_writes_memory_and_reports_to_the_supervisor(self):
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

        # The student no longer writes up unilaterally: the professor
        # decides whether the work is ready (autoprof/supervision.py).
        followup = conn.execute(
            "SELECT * FROM jobs WHERE kind='professor_supervision'"
        ).fetchall()
        self.assertEqual(len(followup), 1)
        self.assertEqual(followup[0]["target_id"], ids["task_id"])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='student_write_paper'").fetchone()[0],
            0,
        )
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


class EmptyWorkOutputTests(unittest.TestCase):
    def test_empty_work_output_fails_without_erasing_memory(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _enqueue(conn, "student_work", ids["task_id"])
        backend = ScriptedBackend(BackendResult(text="   "))

        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            memory = lab_dir / ids["student_memory_path"]
            memory.parent.mkdir(parents=True)
            memory.write_text("hard-won prior research")

            outcome = paper.execute_student_work_job(conn, job_id, backend, lab_dir)

            self.assertIn(outcome, ("retrying", "failed"))
            self.assertEqual(memory.read_text(), "hard-won prior research")

        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='student_write_paper'").fetchone()[0],
            0,
        )
        conn.close()


class RevisePaperJobTests(unittest.TestCase):
    def _rejected_paper(self, conn, ids, lab_dir):
        cur = conn.execute(
            "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
            "VALUES (?, ?, 'p.html', 'Old Title', 'rejected', 1)",
            (ids["task_id"], ids["student_id"]),
        )
        paper_id = cur.lastrowid
        relpath = f"{ids['lab_id']}/tasks/{ids['task_id']}/papers/{paper_id}/paper.html"
        conn.execute("UPDATE papers SET path=? WHERE id=?", (relpath, paper_id))
        f = lab_dir / relpath
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("<h1>Old Title</h1><p>v1</p>")

        for i in (1, 2, 3):
            rp = f"{ids['lab_id']}/reviews/p{paper_id}/{i}.md"
            (lab_dir / rp).parent.mkdir(parents=True, exist_ok=True)
            (lab_dir / rp).write_text(f"reviewer {i}: fix citation [2]")
            conn.execute(
                "INSERT INTO reviews (target_type,target_id,review_round,reviewer_index,verdict,rationale_path) "
                "VALUES ('paper',?,1,?,'weak_accept',?)",
                (paper_id, i, rp),
            )
        conn.commit()
        return paper_id

    def _enqueue_revise(self, conn, paper_id):
        cur = conn.execute(
            "INSERT INTO jobs (kind,target_type,target_id,status) "
            "VALUES ('student_revise_paper','paper',?,'pending')",
            (paper_id,),
        )
        conn.commit()
        return cur.lastrowid

    def test_revision_overwrites_paper_and_starts_round_two(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            paper_id = self._rejected_paper(conn, ids, lab_dir)
            job_id = self._enqueue_revise(conn, paper_id)
            backend = ScriptedBackend(
                BackendResult(text="<title>New Title</title><h1>New Title</h1><p>v2</p>")
            )

            outcome = paper.execute_student_revise_paper_job(conn, job_id, backend, lab_dir)
            self.assertEqual(outcome, "done")

            row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            self.assertEqual(row["status"], "in_review")
            self.assertEqual(row["review_round"], 2)
            self.assertEqual(row["title"], "New Title")
            self.assertIn("v2", (lab_dir / row["path"]).read_text())

            new_reviews = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE kind='paper_review' AND review_round=2"
            ).fetchone()[0]
            self.assertEqual(new_reviews, 3)

            # The prompt must actually carry the reviewers' objections.
            self.assertIn("fix citation [2]", backend.calls[0])
        conn.close()

    def test_round_one_reviews_are_preserved(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            paper_id = self._rejected_paper(conn, ids, lab_dir)
            job_id = self._enqueue_revise(conn, paper_id)
            paper.execute_student_revise_paper_job(
                conn, job_id, ScriptedBackend(BackendResult(text="<h1>New</h1>")), lab_dir
            )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM reviews WHERE target_type='paper' AND review_round=1"
            ).fetchone()[0],
            3,
        )
        conn.close()

    def test_non_rejected_paper_is_a_noop(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            paper_id = self._rejected_paper(conn, ids, lab_dir)
            conn.execute("UPDATE papers SET status='accepted' WHERE id=?", (paper_id,))
            conn.commit()
            job_id = self._enqueue_revise(conn, paper_id)
            backend = ScriptedBackend(BackendResult(text="<h1>New</h1>"))
            outcome = paper.execute_student_revise_paper_job(conn, job_id, backend, lab_dir)

        self.assertEqual(outcome, "done")
        self.assertEqual(backend.calls, [])
        conn.close()

    def test_non_html_revision_fails_without_destroying_the_paper(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            paper_id = self._rejected_paper(conn, ids, lab_dir)
            job_id = self._enqueue_revise(conn, paper_id)
            outcome = paper.execute_student_revise_paper_job(
                conn, job_id, ScriptedBackend(BackendResult(text="sorry, cannot")), lab_dir
            )
            self.assertIn(outcome, ("retrying", "failed"))
            row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            self.assertEqual(row["status"], "rejected")
            self.assertIn("v1", (lab_dir / row["path"]).read_text())
        conn.close()


class ExpositionRequirementTests(unittest.TestCase):
    """Papers must read as human-written and use visuals where they earn
    their space -- rubric criterion 5."""

    def test_write_up_prompt_demands_figures_and_narrative(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            job_id = _enqueue(conn, "student_write_paper", ids["task_id"])
            backend = ScriptedBackend(BackendResult(text=MINIMAL_PAPER))
            paper.execute_student_write_paper_job(conn, job_id, backend, Path(d))
        prompt = backend.calls[0]
        self.assertIn("inline <svg>", prompt)
        self.assertIn("as a person would", prompt)
        self.assertIn("greyscale", prompt)
        conn.close()

    def test_template_carries_the_validated_series_colours(self):
        template = paper._PAPER_TEMPLATE_PATH.read_text()
        for hexcode in ("#2a78d6", "#eb6834", "#1baf7a", "#eda100"):
            self.assertIn(hexcode, template)
        # One y-axis per plot is a hard rule, not a suggestion.
        self.assertIn("One y-axis per plot", template)


if __name__ == "__main__":
    unittest.main()
