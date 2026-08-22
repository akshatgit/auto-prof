"""Tests for `autoprof status`'s tree rendering."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof.status_cli import render_blocked, render_status  # noqa: E402
from tests.helpers import fresh_db, seed_lab_with_student  # noqa: E402


class RenderStatusTests(unittest.TestCase):
    def test_empty_db_says_so(self):
        conn = fresh_db()
        self.assertIn("no labs yet", render_status(conn))
        conn.close()

    def test_shows_lab_professor_task_and_student(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        out = render_status(conn)
        self.assertIn(f"LAB #{ids['lab_id']}", out)
        self.assertIn("Prof Test", out)
        self.assertIn(f"TASK #{ids['task_id']}", out)
        self.assertIn(f"student #{ids['student_id']}", out)
        conn.close()

    def test_flags_a_paused_student(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        conn.execute(
            "UPDATE students SET paused_at = datetime('now') WHERE id = ?", (ids["student_id"],)
        )
        conn.commit()
        self.assertIn("PAUSED", render_status(conn))
        conn.close()

    def test_shows_paper_and_its_review_tally(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        cur = conn.execute(
            "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
            "VALUES (?, ?, 'p.html', 'A Result', 'in_review', 1)",
            (ids["task_id"], ids["student_id"]),
        )
        paper_id = cur.lastrowid
        for i, verdict in enumerate(["strong_accept", "strong_accept", "reject"], start=1):
            conn.execute(
                "INSERT INTO reviews (target_type, target_id, review_round, reviewer_index, "
                "verdict, rationale_path) VALUES ('paper', ?, 1, ?, ?, 'r.md')",
                (paper_id, i, verdict),
            )
        conn.commit()

        out = render_status(conn)
        self.assertIn(f"PAPER #{paper_id}", out)
        self.assertIn("A Result", out)
        self.assertIn("2 strong_accept", out)
        conn.close()

    def test_reports_job_queue_and_failures(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('student_work', 'task', ?, 'pending')",
            (ids["task_id"],),
        )
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status, last_error) "
            "VALUES ('student_write_paper', 'task', ?, 'failed', 'no VERDICT line found')",
            (ids["task_id"],),
        )
        conn.commit()

        out = render_status(conn)
        self.assertIn("pending=1", out)
        self.assertIn("failed=1", out)
        self.assertIn("no VERDICT line found", out)
        conn.close()

    def test_lab_review_tally_is_shown_for_the_current_round(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        for i, verdict in enumerate(["strong_reject"] * 3, start=1):
            conn.execute(
                "INSERT INTO reviews (target_type, target_id, review_round, reviewer_index, "
                "verdict, rationale_path) VALUES ('lab', ?, 1, ?, ?, 'r.md')",
                (ids["lab_id"], i, verdict),
            )
        conn.commit()
        out = render_status(conn)
        self.assertIn("reviews[r1]", out)
        self.assertIn("0 strong_accept", out)
        conn.close()


class RenderBlockedTests(unittest.TestCase):
    """`status --blocked` exists because a provider refusal leaves no
    failing test and no pending job -- the lab just silently stops."""

    CYBER = ("This content was flagged for possible cybersecurity risk. To get "
             "authorized for security work, join the Trusted Access for Cyber program")

    def _failed_job(self, conn, ids, kind, error):
        cur = conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status, last_error) "
            "VALUES (?, 'task', ?, 'failed', ?)", (kind, ids["task_id"], error))
        conn.commit()
        return cur.lastrowid

    def test_quiet_when_nothing_is_blocked(self):
        conn = fresh_db()
        self.assertIn("No blocked jobs", render_blocked(conn))
        conn.close()

    def test_reports_a_refused_job_with_the_remedy(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = self._failed_job(conn, ids, "professor_supervision", self.CYBER)
        out = render_blocked(conn)
        self.assertIn(f"job {job_id}", out)
        self.assertIn("model_denied", out)
        self.assertIn("refused by the provider on content grounds", out)
        # The remedy must be actionable, not just a label.
        self.assertIn("different provider", out)
        conn.close()

    def test_deliberate_cancellations_are_not_reported(self):
        # 39 operator supersessions once buried the single real refusal.
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        self._failed_job(conn, ids, "student_work", "superseded: restart with fresh session")
        self._failed_job(conn, ids, "student_work", "lab 6 retired: ran with broken tooling")
        self.assertIn("No blocked jobs", render_blocked(conn))
        conn.close()


if __name__ == "__main__":
    unittest.main()


class RunningJobProgressTests(unittest.TestCase):
    """Status must distinguish a working job from a hung one."""

    def _job(self, conn, **kw):
        cols = "kind,target_type,target_id,status,started_at"
        vals = ["student_work", "task", 1, "running", "2026-08-22 10:00:00"]
        for k, v in kw.items():
            cols += "," + k
            vals.append(v)
        conn.execute(f"INSERT INTO jobs ({cols}) VALUES ({','.join('?' * len(vals))})", vals)
        conn.commit()

    def test_a_working_job_reports_tokens(self):
        conn = fresh_db()
        seed_lab_with_student(conn)
        self._job(conn, progress_at="2026-08-22 10:05:00", progress_tokens=4210,
                  progress_items=7)
        out = render_status(conn)
        self.assertIn("4210 tokens", out)
        self.assertIn("7 items", out)
        conn.close()

    def test_a_silent_job_says_so(self):
        conn = fresh_db()
        seed_lab_with_student(conn)
        self._job(conn)
        out = render_status(conn)
        self.assertIn("no output yet", out)
        conn.close()
