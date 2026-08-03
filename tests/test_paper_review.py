"""Tests for the 3-reviewer / 2-of-3 paper review pipeline."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import paper_review  # noqa: E402
from autoprof.backends.base import Backend, BackendResult  # noqa: E402
from tests.helpers import fresh_db, seed_lab_with_student  # noqa: E402


class ScriptedBackend(Backend):
    name = "scripted"

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def run(self, prompt, **opts):
        self.calls.append(prompt)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def _seed_paper(conn, ids, lab_dir: Path, status="in_review") -> int:
    cur = conn.execute(
        "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
        "VALUES (?, ?, 'pending', 'A Paper', ?, 1)",
        (ids["task_id"], ids["student_id"], status),
    )
    paper_id = cur.lastrowid
    relpath = f"{ids['lab_id']}/tasks/{ids['task_id']}/papers/{paper_id}/paper.html"
    conn.execute("UPDATE papers SET path = ? WHERE id = ?", (relpath, paper_id))
    conn.execute("UPDATE students SET status = 'in_review' WHERE id = ?", (ids["student_id"],))
    conn.commit()

    paper_file = lab_dir / relpath
    paper_file.parent.mkdir(parents=True, exist_ok=True)
    paper_file.write_text("<h1>A Paper</h1><p>the argument</p>")
    return paper_id


def _verdict(v: str) -> BackendResult:
    return BackendResult(text=f"Novelty: fine.\nCorrectness: fine.\n\nVERDICT: {v}")


class BuildReviewPromptTests(unittest.TestCase):
    def test_substitutes_document_and_type(self):
        prompt = paper_review.build_review_prompt("<h1>Doc</h1>", "a research paper")
        self.assertIn("<h1>Doc</h1>", prompt)
        self.assertIn("a research paper", prompt)
        self.assertNotIn("{DOCUMENT_CONTENT}", prompt)
        self.assertNotIn("{DOCUMENT_TYPE}", prompt)

    def test_survives_css_braces_in_the_document(self):
        # The regression that str.format() would have caused: an ACM-style
        # HTML paper is full of CSS braces, which format() reads as fields.
        css = "<style>body { column-count: 2; }</style><h1>T</h1>"
        prompt = paper_review.build_review_prompt(css)
        self.assertIn("column-count: 2", prompt)

    def test_strips_the_rubric_authoring_comment(self):
        prompt = paper_review.build_review_prompt("<h1>Doc</h1>")
        self.assertNotIn("auto-prof review rubric", prompt)
        self.assertIn("VERDICT:", prompt)

    def test_carries_the_kill_mandate(self):
        # Papers here passed by outlasting the panel, not by being good:
        # 0 of 18 strong_accepts in round 1, 10 of 15 by round 4. The
        # rubric must instruct an attack, demand a falsification test, and
        # say outright that surviving revision rounds is not a reason to
        # soften -- a reviewer with no memory of prior rounds otherwise
        # reads a well-patched paper as a strong one.
        prompt = paper_review.build_review_prompt("<h1>Doc</h1>")
        self.assertIn("kill", prompt.lower())
        self.assertIn("counterexample", prompt.lower())
        self.assertIn("falsif", prompt.lower())
        self.assertIn("Revision is not a reason to soften", prompt)


class RequestPaperReviewTests(unittest.TestCase):
    def test_enqueues_three_jobs_for_the_current_round(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            paper_id = _seed_paper(conn, ids, Path(d))
        job_ids = paper_review.request_paper_review(conn, paper_id)
        self.assertEqual(len(job_ids), 3)
        rows = conn.execute("SELECT * FROM jobs WHERE kind='paper_review'").fetchall()
        self.assertEqual(sorted(r["reviewer_index"] for r in rows), [1, 2, 3])
        conn.close()

    def test_double_request_is_rejected(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            paper_id = _seed_paper(conn, ids, Path(d))
        paper_review.request_paper_review(conn, paper_id)
        with self.assertRaises(paper_review.PaperReviewError):
            paper_review.request_paper_review(conn, paper_id)
        conn.close()


class ExecutePaperReviewJobTests(unittest.TestCase):
    def _review_all(self, conn, ids, lab_dir, verdicts):
        paper_id = _seed_paper(conn, ids, lab_dir)
        job_ids = paper_review.request_paper_review(conn, paper_id)
        for job_id, verdict in zip(job_ids, verdicts):
            backend = ScriptedBackend([_verdict(verdict)])
            paper_review.execute_paper_review_job(conn, job_id, backend, lab_dir)
        return paper_id

    def test_records_verdict_and_rationale_file(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            paper_id = _seed_paper(conn, ids, lab_dir)
            job_ids = paper_review.request_paper_review(conn, paper_id)
            outcome = paper_review.execute_paper_review_job(
                conn, job_ids[0], ScriptedBackend([_verdict("accept")]), lab_dir
            )
            self.assertEqual(outcome, "done")
            review = conn.execute("SELECT * FROM reviews").fetchone()
            self.assertEqual(review["verdict"], "accept")
            self.assertTrue((lab_dir / review["rationale_path"]).exists())
        conn.close()

    def test_two_of_three_strong_accept_accepts_the_paper(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            paper_id = self._review_all(
                conn, ids, lab_dir, ["strong_accept", "weak_reject", "strong_accept"]
            )
        row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        self.assertEqual(row["status"], "accepted")
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (ids["task_id"],)).fetchone()
        self.assertEqual(task["status"], "pending_prof_review")
        conn.close()

    def test_one_strong_accept_rejects_the_paper(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            paper_id = self._review_all(
                conn, ids, lab_dir, ["strong_accept", "accept", "accept"]
            )
        row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        self.assertEqual(row["status"], "rejected")
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (ids["task_id"],)).fetchone()
        self.assertNotEqual(task["status"], "pending_prof_review")
        conn.close()

    def test_no_tally_until_all_three_reviews_land(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            paper_id = _seed_paper(conn, ids, lab_dir)
            job_ids = paper_review.request_paper_review(conn, paper_id)
            for job_id in job_ids[:2]:
                paper_review.execute_paper_review_job(
                    conn, job_id, ScriptedBackend([_verdict("strong_accept")]), lab_dir
                )
            row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            self.assertEqual(row["status"], "in_review")
        conn.close()

    def test_takes_the_last_verdict_line(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        text = (
            "I will end with a line of the form\nVERDICT: strong_accept\n"
            "...but that was only an example.\n\nVERDICT: reject\n"
        )
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            paper_id = _seed_paper(conn, ids, lab_dir)
            job_ids = paper_review.request_paper_review(conn, paper_id)
            paper_review.execute_paper_review_job(
                conn, job_ids[0], ScriptedBackend([BackendResult(text=text)]), lab_dir
            )
        review = conn.execute("SELECT * FROM reviews").fetchone()
        self.assertEqual(review["verdict"], "reject")
        conn.close()

    def test_missing_verdict_line_fails_the_job(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            paper_id = _seed_paper(conn, ids, lab_dir)
            job_ids = paper_review.request_paper_review(conn, paper_id)
            outcome = paper_review.execute_paper_review_job(
                conn, job_ids[0], ScriptedBackend([BackendResult(text="no verdict here")]), lab_dir
            )
        self.assertIn(outcome, ("retrying", "failed"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 0)
        conn.close()

    def test_stale_round_job_fails_instead_of_hitting_the_trigger(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            paper_id = _seed_paper(conn, ids, lab_dir)
            job_ids = paper_review.request_paper_review(conn, paper_id)
            conn.execute("UPDATE papers SET review_round = 2 WHERE id = ?", (paper_id,))
            conn.commit()
            outcome = paper_review.execute_paper_review_job(
                conn, job_ids[0], ScriptedBackend([_verdict("accept")]), lab_dir
            )
        self.assertIn(outcome, ("retrying", "failed"))
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_ids[0],)).fetchone()
        self.assertIn("round", row["last_error"])
        conn.close()

    def test_missing_paper_file_fails_the_job(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            paper_id = _seed_paper(conn, ids, lab_dir)
            (lab_dir / conn.execute(
                "SELECT path FROM papers WHERE id=?", (paper_id,)
            ).fetchone()["path"]).unlink()
            job_ids = paper_review.request_paper_review(conn, paper_id)
            outcome = paper_review.execute_paper_review_job(
                conn, job_ids[0], ScriptedBackend([_verdict("accept")]), lab_dir
            )
        self.assertIn(outcome, ("retrying", "failed"))
        conn.close()


class ResubmitPaperTests(unittest.TestCase):
    def test_bumps_round_and_enqueues_a_fresh_reviewer_set(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            paper_id = _seed_paper(conn, ids, lab_dir)
            job_ids = paper_review.request_paper_review(conn, paper_id)
            for job_id in job_ids:
                paper_review.execute_paper_review_job(
                    conn, job_id, ScriptedBackend([_verdict("reject")]), lab_dir
                )

            new_jobs = paper_review.resubmit_paper(conn, paper_id)

        row = conn.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
        self.assertEqual(row["review_round"], 2)
        self.assertEqual(row["status"], "in_review")
        self.assertEqual(len(new_jobs), 3)
        # Round 1's reviews are history, not overwritten.
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM reviews WHERE target_type='paper' AND review_round=1"
            ).fetchone()[0],
            3,
        )
        conn.close()

    def test_refuses_to_resubmit_a_paper_that_was_not_rejected(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            paper_id = _seed_paper(conn, ids, Path(d))
        with self.assertRaises(paper_review.PaperReviewError):
            paper_review.resubmit_paper(conn, paper_id)
        conn.close()


class RevisionEnqueueTests(unittest.TestCase):
    def _reject(self, conn, ids, lab_dir, paper_id=None):
        if paper_id is None:
            paper_id = _seed_paper(conn, ids, lab_dir)
        job_ids = paper_review.request_paper_review(conn, paper_id)
        for job_id in job_ids:
            paper_review.execute_paper_review_job(
                conn, job_id, ScriptedBackend([_verdict("weak_accept")]), lab_dir
            )
        return paper_id

    def test_rejection_enqueues_a_revision_job(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            paper_id = self._reject(conn, ids, Path(d))

        rows = conn.execute(
            "SELECT * FROM jobs WHERE kind='student_revise_paper'"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target_type"], "paper")
        self.assertEqual(rows[0]["target_id"], paper_id)
        conn.close()

    def test_acceptance_enqueues_no_revision(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            paper_id = _seed_paper(conn, ids, lab_dir)
            job_ids = paper_review.request_paper_review(conn, paper_id)
            for job_id in job_ids:
                paper_review.execute_paper_review_job(
                    conn, job_id, ScriptedBackend([_verdict("strong_accept")]), lab_dir
                )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='student_revise_paper'").fetchone()[0],
            0,
        )
        conn.close()

    def test_no_revision_once_the_lab_hits_its_accepted_paper_target(self):
        """The loop stops on what the lab produced, not on rounds spent."""
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            for _ in range(paper_review.config.max_accepted_papers()):
                conn.execute(
                    "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
                    "VALUES (?, ?, 'x.html', 'done', 'accepted', 1)",
                    (ids["task_id"], ids["student_id"]),
                )
            conn.commit()
            self._reject(conn, ids, lab_dir)

        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='student_revise_paper'").fetchone()[0],
            0,
        )
        conn.close()

    def test_revision_continues_below_the_target_regardless_of_round(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            paper_id = _seed_paper(conn, ids, lab_dir)
            # Round 9 -- far past any old round cap; with zero accepted
            # papers the lab must still keep trying.
            conn.execute("UPDATE papers SET review_round=9 WHERE id=?", (paper_id,))
            conn.commit()
            self._reject(conn, ids, lab_dir, paper_id=paper_id)

        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='student_revise_paper'").fetchone()[0],
            1,
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()
