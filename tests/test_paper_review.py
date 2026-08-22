"""Tests for the 3-reviewer / 2-of-3 paper review pipeline."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import config, paper_review  # noqa: E402
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

    def test_defines_the_verdict_tiers(self):
        # The kill mandate without tier definitions collapsed every review
        # onto strong_reject -- 9 of 9, against a prior spread across all
        # six verdicts. A reviewer returning one constant verdict has
        # stopped discriminating, so the rubric must say what separates
        # an unrecoverable defect from a fixable one.
        prompt = paper_review.build_review_prompt("<h1>Doc</h1>")
        self.assertIn("cannot be repaired by revision", prompt)
        self.assertIn("you may not return `strong_reject`", prompt)

    def test_tells_the_reviewer_what_its_verdict_actually_does(self):
        # Only `strong_accept` admits a document, but the sole mention of
        # that gate lived in the authoring comment, which is stripped --
        # so reviewers graded on a six-point scale without knowing five
        # of the tiers were the same outcome. Codex and Claude returned 1
        # strong_accept in 292 reviews but 15 accept-tier verdicts, and a
        # paper that went accept/accept/accept was recorded as rejected.
        prompt = paper_review.build_review_prompt("<h1>Doc</h1>")
        self.assertIn("Only `strong_accept` admits", prompt)
        self.assertIn("including `accept`", prompt)

    def test_does_not_leak_panel_size_or_the_tally_rule(self):
        # Reviewer independence is the load-bearing property: a reviewer
        # who knows the threshold can vote strategically toward it.
        prompt = paper_review.build_review_prompt("<h1>Doc</h1>")
        self.assertNotIn("2-of-3", prompt)
        self.assertNotIn("4-of-5", prompt)


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

    def test_task_is_abandoned_once_it_hits_the_rejected_paper_cap(self):
        # The loop that produced 29 rejected papers on task #4. Neither
        # existing cap could stop it: max_accepted_papers counts successes
        # and this task has none, and the supervision cap forces a
        # write-up whose rejection re-enters supervision.
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        cap = config.max_rejected_papers()
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            for _ in range(cap):
                self._reject(conn, ids, lab_dir)

        task = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (ids["task_id"],)
        ).fetchone()
        self.assertEqual(task["status"], "abandoned")
        # and the last rejection must NOT have queued yet another rewrite
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE kind='student_revise_paper'"
            ).fetchone()[0],
            cap - 1,
        )
        conn.close()

    def test_below_the_cap_the_revise_loop_still_runs(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            for _ in range(config.max_rejected_papers() - 1):
                self._reject(conn, ids, lab_dir)

        task = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (ids["task_id"],)
        ).fetchone()
        self.assertNotEqual(task["status"], "abandoned")
        conn.close()

    def test_scoped_zero_rejection_cap_keeps_revising(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(
            paper_review.config, "max_rejected_papers", lambda **_: 0
        ):
            lab_dir = Path(d)
            self._reject(conn, ids, lab_dir)
            self._reject(conn, ids, lab_dir)

        task = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (ids["task_id"],)
        ).fetchone()
        self.assertNotEqual(task["status"], "abandoned")
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE kind='student_revise_paper'"
            ).fetchone()[0],
            2,
        )
        conn.close()

    def test_rejections_on_another_task_do_not_count(self):
        # Per-task, not lab-wide: one task failing must not abandon a
        # sibling task that is doing fine.
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        other = conn.execute(
            "INSERT INTO tasks (lab_id, title, brief_path, direction, end_criteria, status) "
            "VALUES (?, 'Other', 'b.md', 'prove', 'crit', 'in_progress')",
            (ids["lab_id"],),
        ).lastrowid
        prof = conn.execute(
            "SELECT professor_id FROM students WHERE id = ?", (ids["student_id"],)
        ).fetchone()["professor_id"]
        other_student = conn.execute(
            "INSERT INTO students (task_id, professor_id, status, memory_path) "
            "VALUES (?, ?, 'working', 'm.md')",
            (other, prof),
        ).lastrowid
        for title in ("Old", "Old2"):
            conn.execute(
                "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
                "VALUES (?, ?, 'p', ?, 'rejected', 1)",
                (other, other_student, title),
            )
        conn.commit()
        with tempfile.TemporaryDirectory() as d:
            self._reject(conn, ids, Path(d))

        task = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (ids["task_id"],)
        ).fetchone()
        self.assertNotEqual(task["status"], "abandoned")
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

    def test_revision_limit_returns_the_task_to_research(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            paper_id = _seed_paper(conn, ids, lab_dir)
            # The task keeps trying, but it must produce evidence and a new
            # paper instead of indefinitely expanding the same document.
            conn.execute("UPDATE papers SET review_round=9 WHERE id=?", (paper_id,))
            conn.commit()
            self._reject(conn, ids, lab_dir, paper_id=paper_id)

        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='student_revise_paper'").fetchone()[0],
            0,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='student_work'").fetchone()[0],
            1,
        )
        conn.close()


if __name__ == "__main__":
    unittest.main()


class SweepStalledReviewsTests(unittest.TestCase):
    """A failed review must not strand its paper forever."""

    def setUp(self):
        self.conn = fresh_db()
        self.ids = seed_lab_with_student(self.conn)
        cur = self.conn.execute(
            "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
            "VALUES (?, ?, 'p.html', 'T', 'in_review', 1)",
            (self.ids["task_id"], self.ids["student_id"]),
        )
        self.paper_id = cur.lastrowid
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _file_review(self, index, verdict="reject"):
        self.conn.execute(
            "INSERT INTO reviews (target_type, target_id, review_round, reviewer_index, "
            "verdict, rationale_path) VALUES ('paper', ?, 1, ?, ?, 'r.md')",
            (self.paper_id, index, verdict),
        )
        self.conn.commit()

    def _job(self, kind, index, status):
        cur = self.conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, review_round, reviewer_index, "
            "status) VALUES (?, 'paper', ?, 1, ?, ?)",
            (kind, self.paper_id, index, status),
        )
        self.conn.commit()
        return cur.lastrowid

    def _status(self):
        return self.conn.execute(
            "SELECT status FROM papers WHERE id = ?", (self.paper_id,)
        ).fetchone()["status"]

    def test_all_reviews_filed_gets_tallied(self):
        for i in (1, 2, 3):
            self._file_review(i)
        self._job("paper_review", 1, "failed")
        out = paper_review.sweep_stalled_reviews(self.conn)
        self.assertIn(self.paper_id, out["finalized"])
        self.assertEqual(self._status(), "rejected")

    def test_two_strong_accepts_still_pass_through_the_sweep(self):
        self._file_review(1, "strong_accept")
        self._file_review(2, "strong_accept")
        self._file_review(3, "reject")
        self._job("paper_review", 3, "failed")
        paper_review.sweep_stalled_reviews(self.conn)
        self.assertEqual(self._status(), "accepted")

    def test_missing_review_with_a_failed_job_is_requeued(self):
        self._file_review(1)
        self._file_review(2)
        self._job("paper_review", 3, "failed")
        out = paper_review.sweep_stalled_reviews(self.conn)
        self.assertTrue(out["requeued"])
        self.assertEqual(self._status(), "in_review")

    def test_a_live_job_means_no_interference(self):
        for i in (1, 2, 3):
            self._file_review(i)
        self._job("paper_review", 1, "running")
        out = paper_review.sweep_stalled_reviews(self.conn)
        self.assertEqual(out["finalized"], [])
        self.assertEqual(self._status(), "in_review")

    def test_missing_review_without_a_failed_job_is_not_invented(self):
        self._file_review(1)
        out = paper_review.sweep_stalled_reviews(self.conn)
        self.assertEqual(out["requeued"], [])

    def test_unanswered_exchange_is_not_tallied_over(self):
        for i in (1, 2, 3):
            self._file_review(i)
        self.conn.execute(
            "INSERT INTO review_exchanges (target_type, target_id, review_round, "
            "reviewer_index, exchange_round, request_path) "
            "VALUES ('paper', ?, 1, 1, 1, 'q.md')",
            (self.paper_id,),
        )
        self._job("author_response", 1, "failed")
        out = paper_review.sweep_stalled_reviews(self.conn)
        self.assertEqual(out["finalized"], [])
        self.assertTrue(out["requeued"])


class RevisionStallTests(unittest.TestCase):
    """Revision must stop when it stops moving the panel."""

    def setUp(self):
        self.conn = fresh_db()
        self.ids = seed_lab_with_student(self.conn)
        cur = self.conn.execute(
            "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
            "VALUES (?, ?, 'p.html', 'T', 'in_review', 1)",
            (self.ids["task_id"], self.ids["student_id"]),
        )
        self.paper_id = cur.lastrowid
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _round(self, rnd, verdicts):
        # A trigger requires reviews to match the paper's current round.
        self.conn.execute("UPDATE papers SET review_round = ? WHERE id = ?",
                          (rnd, self.paper_id))
        for i, v in enumerate(verdicts, start=1):
            self.conn.execute(
                "INSERT INTO reviews (target_type, target_id, review_round, reviewer_index, "
                "verdict, rationale_path) VALUES ('paper', ?, ?, ?, ?, 'r.md')",
                (self.paper_id, rnd, i, v),
            )
        self.conn.commit()

    def _stalled(self):
        return paper_review._revision_has_stalled(self.conn, self.paper_id)

    def test_too_early_to_judge(self):
        self._round(1, ["reject", "reject", "reject"])
        self.assertFalse(self._stalled())

    def test_steady_improvement_is_not_a_stall(self):
        self._round(1, ["strong_reject"] * 3)
        self._round(2, ["reject"] * 3)
        self._round(3, ["weak_accept", "reject", "reject"])
        self._round(4, ["strong_accept", "reject", "reject"])
        self.assertFalse(self._stalled())

    def test_plateau_after_a_peak_is_a_stall(self):
        # paper 68's actual shape: climbs, then stops dead.
        self._round(1, ["strong_reject", "reject", "reject"])
        self._round(2, ["weak_reject", "strong_accept", "weak_accept"])
        self._round(3, ["reject", "strong_accept", "accept"])
        self._round(4, ["reject", "strong_accept", "accept"])
        self.assertFalse(self._stalled())   # two flat rounds: still allowed
        self._round(5, ["reject", "strong_accept", "accept"])
        self.assertTrue(self._stalled())    # three: revision is not working

    def test_flat_rejection_is_a_stall(self):
        for rnd in (1, 2, 3, 4):
            self._round(rnd, ["reject", "reject", "reject"])
        self.assertTrue(self._stalled())

    def test_a_late_improvement_clears_the_stall(self):
        for rnd in (1, 2, 3):
            self._round(rnd, ["reject"] * 3)
        self._round(4, ["strong_accept", "reject", "reject"])
        self.assertFalse(self._stalled())

    def test_it_reads_the_best_verdict_not_the_average(self):
        # The accept gate counts strong_accepts, so improvement is measured
        # on the best verdict, not on the panel mean.
        self._round(1, ["strong_reject"] * 3)
        self._round(2, ["strong_reject", "strong_reject", "weak_accept"])
        self._round(3, ["strong_reject", "strong_reject", "accept"])
        self._round(4, ["strong_reject", "strong_reject", "strong_accept"])
        self.assertFalse(self._stalled())
