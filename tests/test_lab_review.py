import tempfile
import unittest
from pathlib import Path

from autoprof import lab_review
from autoprof.backends.base import Backend, BackendResult
from tests.helpers import fresh_db


def _seed_lab(conn, status="pending_review"):
    cur = conn.execute(
        "INSERT INTO professors (lab_id, name, field, status, memory_path) "
        "VALUES (NULL, 'Prof', 'Field', 'active', 'mem.md')"
    )
    professor_id = cur.lastrowid
    cur = conn.execute(
        "INSERT INTO labs (professor_id, root_problem, status) VALUES (?, 'is X true?', ?)",
        (professor_id, status),
    )
    lab_id = cur.lastrowid
    conn.execute("UPDATE professors SET lab_id=? WHERE id=?", (lab_id, professor_id))
    conn.commit()
    return {"professor_id": professor_id, "lab_id": lab_id}


class ScriptedBackend(Backend):
    name = "scripted"

    def __init__(self, results):
        # results: list consumed in order, one per .run() call
        self._results = list(results)
        self.calls = []

    def run(self, prompt, **opts):
        self.calls.append(prompt)
        return self._results.pop(0)


class BuildPromptTests(unittest.TestCase):
    def test_uses_lab_specific_rubric_not_the_paper_rubric(self):
        prompt = lab_review._build_prompt("is X true?")
        self.assertIn("is X true?", prompt)
        self.assertIn("Well-posedness", prompt)
        # the paper rubric's paper-specific instruction must NOT leak in --
        # a bare problem statement is not supposed to have a Related Work
        # section or a proof yet.
        self.assertNotIn("Related Work", prompt)


class RequestLabReviewTests(unittest.TestCase):
    def test_creates_three_jobs_for_round_1(self):
        conn = fresh_db()
        ids = _seed_lab(conn)

        job_ids = lab_review.request_lab_review(conn, ids["lab_id"])

        self.assertEqual(len(job_ids), 3)
        rows = conn.execute(
            "SELECT * FROM jobs WHERE kind='lab_review' ORDER BY reviewer_index"
        ).fetchall()
        self.assertEqual([r["reviewer_index"] for r in rows], [1, 2, 3])
        self.assertTrue(all(r["review_round"] == 1 for r in rows))
        self.assertTrue(all(r["target_type"] == "lab" for r in rows))
        self.assertTrue(all(r["target_id"] == ids["lab_id"] for r in rows))
        conn.close()

    def test_missing_lab_raises(self):
        conn = fresh_db()
        with self.assertRaises(lab_review.LabReviewError):
            lab_review.request_lab_review(conn, 999)
        conn.close()

    def test_double_request_same_round_raises(self):
        conn = fresh_db()
        ids = _seed_lab(conn)
        lab_review.request_lab_review(conn, ids["lab_id"])
        with self.assertRaises(lab_review.LabReviewError):
            lab_review.request_lab_review(conn, ids["lab_id"])
        conn.close()


class ExecuteLabReviewJobTests(unittest.TestCase):
    def _run_all_three(self, conn, lab_dir, results):
        ids = _seed_lab(conn)
        job_ids = lab_review.request_lab_review(conn, ids["lab_id"])
        backend = ScriptedBackend(results)
        outcomes = [
            lab_review.execute_lab_review_job(conn, jid, backend, lab_dir) for jid in job_ids
        ]
        return ids, job_ids, backend, outcomes

    def test_single_review_parses_verdict_and_writes_row(self):
        conn = fresh_db()
        ids = _seed_lab(conn)
        job_ids = lab_review.request_lab_review(conn, ids["lab_id"])
        backend = ScriptedBackend(
            [BackendResult(text="some rationale\nVERDICT: strong_accept", model_version="m1")]
        )

        with tempfile.TemporaryDirectory() as d:
            outcome = lab_review.execute_lab_review_job(conn, job_ids[0], backend, Path(d))

        self.assertEqual(outcome, "done")
        row = conn.execute(
            "SELECT * FROM reviews WHERE target_type='lab' AND target_id=?", (ids["lab_id"],)
        ).fetchone()
        self.assertEqual(row["verdict"], "strong_accept")
        self.assertEqual(row["reviewer_index"], 1)
        self.assertEqual(row["review_round"], 1)
        conn.close()

    def test_missing_verdict_line_fails_the_job(self):
        conn = fresh_db()
        ids = _seed_lab(conn)
        job_ids = lab_review.request_lab_review(conn, ids["lab_id"])
        backend = ScriptedBackend([BackendResult(text="no verdict here")])

        with tempfile.TemporaryDirectory() as d:
            outcome = lab_review.execute_lab_review_job(conn, job_ids[0], backend, Path(d))

        self.assertIn(outcome, ("retrying", "failed"))
        conn.close()

    def test_two_of_three_strong_accept_activates_lab_and_enqueues_decompose(self):
        conn = fresh_db()
        with tempfile.TemporaryDirectory() as d:
            ids, job_ids, backend, outcomes = self._run_all_three(
                conn,
                Path(d),
                [
                    BackendResult(text="VERDICT: strong_accept"),
                    BackendResult(text="VERDICT: strong_accept"),
                    BackendResult(text="VERDICT: weak_reject"),
                ],
            )

            self.assertTrue(all(o == "done" for o in outcomes))
            lab = conn.execute("SELECT * FROM labs WHERE id=?", (ids["lab_id"],)).fetchone()
            self.assertEqual(lab["status"], "active")

            decompose_job = conn.execute(
                "SELECT * FROM jobs WHERE kind='professor_decompose'"
            ).fetchone()
            self.assertIsNotNone(decompose_job)
            self.assertEqual(decompose_job["target_type"], "professor")
            self.assertEqual(decompose_job["target_id"], ids["professor_id"])
            self.assertEqual(decompose_job["status"], "pending")
        conn.close()

    def test_less_than_threshold_leaves_lab_pending_review(self):
        conn = fresh_db()
        with tempfile.TemporaryDirectory() as d:
            ids, job_ids, backend, outcomes = self._run_all_three(
                conn,
                Path(d),
                [
                    BackendResult(text="VERDICT: strong_accept"),
                    BackendResult(text="VERDICT: weak_reject"),
                    BackendResult(text="VERDICT: reject"),
                ],
            )

            lab = conn.execute("SELECT * FROM labs WHERE id=?", (ids["lab_id"],)).fetchone()
            self.assertEqual(lab["status"], "pending_review")
            decompose_job = conn.execute(
                "SELECT * FROM jobs WHERE kind='professor_decompose'"
            ).fetchone()
            self.assertIsNone(decompose_job)
        conn.close()

    def test_finalize_does_not_trigger_until_all_three_reported(self):
        conn = fresh_db()
        ids = _seed_lab(conn)
        job_ids = lab_review.request_lab_review(conn, ids["lab_id"])
        backend = ScriptedBackend([BackendResult(text="VERDICT: strong_accept")])

        with tempfile.TemporaryDirectory() as d:
            lab_review.execute_lab_review_job(conn, job_ids[0], backend, Path(d))

        lab = conn.execute("SELECT status FROM labs WHERE id=?", (ids["lab_id"],)).fetchone()
        self.assertEqual(lab["status"], "pending_review")
        conn.close()

    def test_rationale_file_written(self):
        conn = fresh_db()
        ids = _seed_lab(conn)
        job_ids = lab_review.request_lab_review(conn, ids["lab_id"])
        backend = ScriptedBackend([BackendResult(text="my rationale\nVERDICT: accept")])

        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            lab_review.execute_lab_review_job(conn, job_ids[0], backend, lab_dir)
            expected = lab_dir / f"{ids['lab_id']}/reviews/1/1.md"
            self.assertTrue(expected.exists())
            self.assertIn("VERDICT: accept", expected.read_text())
        conn.close()


class ReviseRootProblemTests(unittest.TestCase):
    def test_bumps_round_and_enqueues_a_fresh_reviewer_set(self):
        conn = fresh_db()
        ids = _seed_lab(conn)
        lab_review.request_lab_review(conn, ids["lab_id"])

        job_ids = lab_review.revise_root_problem(conn, ids["lab_id"], "a sharper problem")

        lab = conn.execute("SELECT * FROM labs WHERE id=?", (ids["lab_id"],)).fetchone()
        self.assertEqual(lab["root_problem"], "a sharper problem")
        self.assertEqual(lab["current_review_round"], 2)
        self.assertEqual(len(job_ids), 3)
        rows = conn.execute(
            "SELECT * FROM jobs WHERE kind='lab_review' AND review_round=2"
        ).fetchall()
        self.assertEqual(sorted(r["reviewer_index"] for r in rows), [1, 2, 3])
        conn.close()

    def test_round_one_jobs_are_left_untouched(self):
        conn = fresh_db()
        ids = _seed_lab(conn)
        first = lab_review.request_lab_review(conn, ids["lab_id"])
        lab_review.revise_root_problem(conn, ids["lab_id"], "a sharper problem")
        still_there = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE kind='lab_review' AND review_round=1"
        ).fetchone()["n"]
        self.assertEqual(still_there, len(first))
        conn.close()

    def test_refuses_to_revise_an_active_lab(self):
        conn = fresh_db()
        ids = _seed_lab(conn)
        conn.execute("UPDATE labs SET status='active' WHERE id=?", (ids["lab_id"],))
        conn.commit()
        with self.assertRaises(lab_review.LabReviewError):
            lab_review.revise_root_problem(conn, ids["lab_id"], "new problem")
        conn.close()

    def test_refuses_an_empty_problem(self):
        conn = fresh_db()
        ids = _seed_lab(conn)
        with self.assertRaises(lab_review.LabReviewError):
            lab_review.revise_root_problem(conn, ids["lab_id"], "   ")
        conn.close()

    def test_unknown_lab_raises(self):
        conn = fresh_db()
        with self.assertRaises(lab_review.LabReviewError):
            lab_review.revise_root_problem(conn, 999, "new problem")
        conn.close()


class AutoRevisionTests(unittest.TestCase):
    """A failed lab review used to be a dead end: the lab sat in
    pending_review with nothing queued, and three labs stranded at once
    before this existed."""

    def _fail_a_round(self, conn, lab_dir):
        ids = _seed_lab(conn)
        job_ids = lab_review.request_lab_review(conn, ids["lab_id"])
        backend = ScriptedBackend([BackendResult(text="rationale\nVERDICT: accept")] * 3)
        for job_id in job_ids:
            lab_review.execute_lab_review_job(conn, job_id, backend, lab_dir)
        return ids

    def test_failed_review_queues_a_revision(self):
        conn = fresh_db()
        with tempfile.TemporaryDirectory() as d:
            ids = self._fail_a_round(conn, Path(d))
        rows = conn.execute("SELECT * FROM jobs WHERE kind='lab_revise'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target_id"], ids["lab_id"])
        conn.close()

    def test_passing_review_queues_no_revision(self):
        conn = fresh_db()
        with tempfile.TemporaryDirectory() as d:
            ids = _seed_lab(conn)
            job_ids = lab_review.request_lab_review(conn, ids["lab_id"])
            backend = ScriptedBackend([BackendResult(text="VERDICT: strong_accept")] * 3)
            for job_id in job_ids:
                lab_review.execute_lab_review_job(conn, job_id, backend, Path(d))
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='lab_revise'").fetchone()[0], 0
        )
        conn.close()

    def test_revision_rewrites_the_problem_and_starts_a_new_round(self):
        conn = fresh_db()
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            ids = self._fail_a_round(conn, lab_dir)
            job_id = conn.execute(
                "SELECT id FROM jobs WHERE kind='lab_revise'"
            ).fetchone()["id"]
            revised = "a sharper root problem " * 40
            lab_review.execute_lab_revise_job(
                conn, job_id, ScriptedBackend([BackendResult(text=revised)]), lab_dir
            )

        lab = conn.execute("SELECT * FROM labs WHERE id=?", (ids["lab_id"],)).fetchone()
        self.assertIn("sharper root problem", lab["root_problem"])
        self.assertEqual(lab["current_review_round"], 2)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE kind='lab_review' AND review_round=2"
            ).fetchone()[0],
            3,
        )
        conn.close()

    def test_a_trivially_short_revision_is_refused(self):
        conn = fresh_db()
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            self._fail_a_round(conn, lab_dir)
            job_id = conn.execute("SELECT id FROM jobs WHERE kind='lab_revise'").fetchone()["id"]
            outcome = lab_review.execute_lab_revise_job(
                conn, job_id, ScriptedBackend([BackendResult(text="try again")]), lab_dir
            )
        self.assertIn(outcome, ("retrying", "failed"))
        conn.close()

    def test_not_queued_twice(self):
        conn = fresh_db()
        with tempfile.TemporaryDirectory() as d:
            ids = self._fail_a_round(conn, Path(d))
        self.assertIsNone(lab_review.request_lab_revision(conn, ids["lab_id"]))
        conn.close()


if __name__ == "__main__":
    unittest.main()
