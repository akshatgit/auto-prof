"""Tests for `autoprof watch` -- notable-event surfacing."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import watch_cli  # noqa: E402
from tests.helpers import fresh_db, seed_lab_with_student  # noqa: E402


def _event(conn, event_type, target_type="paper", target_id=1):
    conn.execute(
        "INSERT INTO events (job_id, actor_type, actor_id, event_type, target_type, target_id) "
        "VALUES (NULL, 'human', NULL, ?, ?, ?)",
        (event_type, target_type, target_id),
    )
    conn.commit()


class CollectTests(unittest.TestCase):
    def test_only_notable_events_surface(self):
        """Routine progress must stay invisible or the signal drowns."""
        conn = fresh_db()
        seed_lab_with_student(conn)
        for kind in ("paper_review_verdict_recorded", "student_worked", "supervision_continue"):
            _event(conn, kind)
        self.assertEqual(watch_cli.collect(conn, 0), [])
        conn.close()

    def test_a_lab_proposal_is_flagged_as_needing_the_human(self):
        conn = fresh_db()
        seed_lab_with_student(conn)
        _event(conn, "lab_proposed", "student", 1)
        [(_, message)] = watch_cli.collect(conn, 0)
        self.assertIn("NEEDS YOU", message)
        self.assertIn("lab proposals", message)
        conn.close()

    def test_acceptance_and_defense_outcomes_surface(self):
        conn = fresh_db()
        seed_lab_with_student(conn)
        for kind in ("paper_accepted", "defense_passed", "job_failed_terminal"):
            _event(conn, kind)
        self.assertEqual(len(watch_cli.collect(conn, 0)), 3)
        conn.close()

    def test_events_are_reported_once(self):
        conn = fresh_db()
        seed_lab_with_student(conn)
        _event(conn, "paper_accepted")
        [(event_id, _)] = watch_cli.collect(conn, 0)
        self.assertEqual(watch_cli.collect(conn, event_id), [])
        conn.close()


class StallWarningTests(unittest.TestCase):
    """The failure that actually happened was silence: a crashed daemon
    produces no events, which looks exactly like a quiet period."""

    def _queued_job(self, conn, ids):
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('student_work', 'task', ?, 'pending')",
            (ids["task_id"],),
        )
        conn.commit()

    def test_warns_when_work_is_queued_but_nothing_completes(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        self._queued_job(conn, ids)
        warning = watch_cli.stall_warning(conn, quiet_seconds=3600, threshold=1800)
        self.assertIsNotNone(warning)
        self.assertIn("daemon may be down", warning)
        conn.close()

    def test_silence_with_an_empty_queue_is_not_a_stall(self):
        """A lab with nothing to do is finished, not broken."""
        conn = fresh_db()
        seed_lab_with_student(conn)
        self.assertIsNone(watch_cli.stall_warning(conn, quiet_seconds=99999, threshold=1800))
        conn.close()

    def test_no_warning_before_the_threshold(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        self._queued_job(conn, ids)
        self.assertIsNone(watch_cli.stall_warning(conn, quiet_seconds=60, threshold=1800))
        conn.close()


if __name__ == "__main__":
    unittest.main()
