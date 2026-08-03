"""Tests for defense, graduation and lab proposal (§3.4/§3.5)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import defense  # noqa: E402
from autoprof.backends.base import Backend, BackendResult  # noqa: E402
from tests.helpers import fresh_db, seed_lab_with_student  # noqa: E402


class ScriptedBackend(Backend):
    name = "scripted"

    def __init__(self, text):
        self.result = BackendResult(text=text)
        self.calls = []

    def run(self, prompt, **opts):
        self.calls.append(prompt)
        return self.result


def _defense(conn, ids, status="in_review"):
    cur = conn.execute(
        "INSERT INTO defenses (student_id, path, status, review_round) VALUES (?, ?, ?, 1)",
        (ids["student_id"], f"{ids['lab_id']}/students/{ids['student_id']}/defense.md", status),
    )
    conn.commit()
    return cur.lastrowid


def _verdict(v):
    return f"a considered review\n\nVERDICT: {v}"


class RequestDefenseTests(unittest.TestCase):
    def test_only_a_nominated_student_writes_a_dissertation(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        self.assertIsNone(defense.request_defense(conn, ids["student_id"]))

        conn.execute("UPDATE students SET status='defending' WHERE id=?", (ids["student_id"],))
        conn.commit()
        self.assertIsNotNone(defense.request_defense(conn, ids["student_id"]))
        conn.close()

    def test_not_queued_twice(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        conn.execute("UPDATE students SET status='defending' WHERE id=?", (ids["student_id"],))
        conn.commit()
        defense.request_defense(conn, ids["student_id"])
        self.assertIsNone(defense.request_defense(conn, ids["student_id"]))
        conn.close()


class DefenseReviewTests(unittest.TestCase):
    def _review_all(self, conn, ids, lab_dir, verdicts):
        defense_id = _defense(conn, ids)
        path = lab_dir / f"{ids['lab_id']}/students/{ids['student_id']}/defense.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("a dissertation " * 400)

        job_ids = defense.request_defense_review(conn, defense_id)
        for job_id, verdict in zip(job_ids, verdicts):
            defense.execute_defense_review_job(
                conn, job_id, ScriptedBackend(_verdict(verdict)), lab_dir
            )
        return defense_id

    def test_five_reviewers_are_requested(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        defense_id = _defense(conn, ids)
        job_ids = defense.request_defense_review(conn, defense_id)
        self.assertEqual(len(job_ids), defense.REVIEWER_COUNT)
        self.assertEqual(defense.REVIEWER_COUNT, 5)
        conn.close()

    def test_four_of_five_strong_accept_graduates_the_student(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            defense_id = self._review_all(
                conn, ids, Path(d),
                ["strong_accept"] * 4 + ["weak_reject"],
            )
        self.assertEqual(
            conn.execute("SELECT status FROM defenses WHERE id=?", (defense_id,)).fetchone()[0],
            "passed",
        )
        self.assertEqual(
            conn.execute("SELECT status FROM students WHERE id=?", (ids["student_id"],)).fetchone()[0],
            "graduated",
        )
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM jobs WHERE status='pending'")]
        self.assertIn("propose_lab", kinds)
        conn.close()

    def test_three_of_five_is_not_enough(self):
        """A defense that slips through founds a lab that generates wrong
        papers for years, so the bar is deliberately above a paper's."""
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            defense_id = self._review_all(
                conn, ids, Path(d),
                ["strong_accept"] * 3 + ["accept", "accept"],
            )
        self.assertEqual(
            conn.execute("SELECT status FROM defenses WHERE id=?", (defense_id,)).fetchone()[0],
            "failed",
        )
        self.assertEqual(
            conn.execute("SELECT status FROM students WHERE id=?", (ids["student_id"],)).fetchone()[0],
            "working",
        )
        conn.close()

    def test_no_tally_until_all_five_report(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            defense_id = _defense(conn, ids)
            path = lab_dir / f"{ids['lab_id']}/students/{ids['student_id']}/defense.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x " * 400)
            job_ids = defense.request_defense_review(conn, defense_id)
            for job_id in job_ids[:4]:
                defense.execute_defense_review_job(
                    conn, job_id, ScriptedBackend(_verdict("strong_accept")), lab_dir
                )
            self.assertEqual(
                conn.execute("SELECT status FROM defenses WHERE id=?", (defense_id,)).fetchone()[0],
                "in_review",
            )
        conn.close()

    def test_missing_verdict_fails_the_job(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            defense_id = _defense(conn, ids)
            path = lab_dir / f"{ids['lab_id']}/students/{ids['student_id']}/defense.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x " * 400)
            job_ids = defense.request_defense_review(conn, defense_id)
            outcome = defense.execute_defense_review_job(
                conn, job_ids[0], ScriptedBackend("no verdict"), lab_dir
            )
        self.assertIn(outcome, ("retrying", "failed"))
        conn.close()


class WriteDefenseTests(unittest.TestCase):
    def _enqueue(self, conn, student_id):
        cur = conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('student_write_defense', 'student', ?, 'pending')",
            (student_id,),
        )
        conn.commit()
        return cur.lastrowid

    def test_a_too_short_dissertation_is_refused(self):
        """~50 words is not a dissertation; accepting it would waste five
        long reviews."""
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = self._enqueue(conn, ids["student_id"])
        with tempfile.TemporaryDirectory() as d:
            outcome = defense.execute_student_write_defense_job(
                conn, job_id, ScriptedBackend("too short"), Path(d)
            )
        self.assertIn(outcome, ("retrying", "failed"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM defenses").fetchone()[0], 0)
        conn.close()

    def test_writes_the_dissertation_and_requests_five_reviews(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = self._enqueue(conn, ids["student_id"])
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            outcome = defense.execute_student_write_defense_job(
                conn, job_id, ScriptedBackend("dissertation body " * 400), lab_dir
            )
            self.assertEqual(outcome, "done")
            row = conn.execute("SELECT * FROM defenses").fetchone()
            self.assertEqual(row["status"], "in_review")
            self.assertTrue((lab_dir / row["path"]).exists())
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='defense_review'").fetchone()[0], 5
        )
        conn.close()


class ProposeLabTests(unittest.TestCase):
    def _enqueue(self, conn, student_id):
        cur = conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('propose_lab', 'student', ?, 'pending')",
            (student_id,),
        )
        conn.commit()
        return cur.lastrowid

    def test_creates_a_pending_proposal_and_no_lab(self):
        """Graduation stops at a PROPOSAL: auto-founding labs on a review
        vote is how a system becomes labs nobody chose to start."""
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        _defense(conn, ids, status="passed")
        conn.execute("UPDATE students SET status='graduated' WHERE id=?", (ids["student_id"],))
        conn.commit()

        job_id = self._enqueue(conn, ids["student_id"])
        payload = json.dumps({
            "name": "Professor New", "field": "A Field", "root_problem": "An open question."
        })
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            path = lab_dir / f"{ids['lab_id']}/students/{ids['student_id']}/defense.md"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("dissertation")
            outcome = defense.execute_propose_lab_job(
                conn, job_id, ScriptedBackend(payload), lab_dir
            )

        self.assertEqual(outcome, "done")
        row = conn.execute("SELECT * FROM lab_proposals").fetchone()
        self.assertEqual(row["status"], "pending_approval")
        self.assertEqual(row["proposed_name"], "Professor New")
        self.assertIsNone(row["resulting_lab_id"])
        # No new lab was created.
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM labs").fetchone()[0], 1)
        conn.close()

    def test_without_a_passed_defense_the_job_fails(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = self._enqueue(conn, ids["student_id"])
        with tempfile.TemporaryDirectory() as d:
            outcome = defense.execute_propose_lab_job(
                conn, job_id, ScriptedBackend("{}"), Path(d)
            )
        self.assertIn(outcome, ("retrying", "failed"))
        conn.close()

    def test_proposal_is_not_duplicated(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        _defense(conn, ids, status="passed")
        conn.execute(
            "INSERT INTO lab_proposals (student_id, proposed_name, proposed_field, "
            "proposed_problem, status) VALUES (?, 'X', 'Y', 'Z', 'pending_approval')",
            (ids["student_id"],),
        )
        conn.commit()
        job_id = self._enqueue(conn, ids["student_id"])
        with tempfile.TemporaryDirectory() as d:
            outcome = defense.execute_propose_lab_job(
                conn, job_id, ScriptedBackend("{}"), Path(d)
            )
        self.assertEqual(outcome, "done")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM lab_proposals").fetchone()[0], 1)
        conn.close()


if __name__ == "__main__":
    unittest.main()
