import unittest

from autoprof import jobs
from tests.helpers import fresh_db, seed_lab_with_student


def _insert_pending_job(conn, task_id, max_attempts=5, kind="student_work"):
    cur = conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status, max_attempts) "
        "VALUES (?, 'task', ?, 'pending', ?)",
        (kind, task_id, max_attempts),
    )
    conn.commit()
    return cur.lastrowid


class BackoffHelperTests(unittest.TestCase):
    def test_error_backoff_grows_and_caps(self):
        b1 = jobs.compute_error_backoff_seconds(1)
        b2 = jobs.compute_error_backoff_seconds(2)
        b3 = jobs.compute_error_backoff_seconds(10)
        self.assertLess(b1, b2)
        self.assertLessEqual(b3, jobs.MAX_ERROR_BACKOFF_SECONDS)

    def test_rate_limit_backoff_uses_explicit_duration_if_given(self):
        self.assertEqual(jobs.compute_rate_limit_backoff_seconds(1, explicit_seconds=42.0), 42.0)

    def test_rate_limit_backoff_grows_and_caps_without_explicit_duration(self):
        b1 = jobs.compute_rate_limit_backoff_seconds(1, explicit_seconds=None)
        b2 = jobs.compute_rate_limit_backoff_seconds(2, explicit_seconds=None)
        b5 = jobs.compute_rate_limit_backoff_seconds(20, explicit_seconds=None)
        self.assertLess(b1, b2)
        self.assertLessEqual(b5, jobs.MAX_RATE_LIMIT_BACKOFF_SECONDS)


class ClaimJobTests(unittest.TestCase):
    def test_claims_pending_job(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"])

        claimed = jobs.claim_job(conn, job_id, lease_id="lease-1", lease_seconds=60)

        self.assertTrue(claimed)
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertEqual(row["lease_id"], "lease-1")
        self.assertIsNotNone(row["lease_expires_at"])
        self.assertIsNotNone(row["started_at"])
        conn.close()

    def test_cannot_double_claim(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"])

        first = jobs.claim_job(conn, job_id, lease_id="lease-1", lease_seconds=60)
        second = jobs.claim_job(conn, job_id, lease_id="lease-2", lease_seconds=60)

        self.assertTrue(first)
        self.assertFalse(second)
        row = conn.execute("SELECT lease_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["lease_id"], "lease-1")
        conn.close()

    def test_cannot_claim_job_not_yet_eligible(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"])
        conn.execute(
            "UPDATE jobs SET not_before = datetime('now', '+1 hour') WHERE id=?", (job_id,)
        )
        conn.commit()

        claimed = jobs.claim_job(conn, job_id, lease_id="lease-1", lease_seconds=60)
        self.assertFalse(claimed)
        conn.close()

    def test_can_claim_job_whose_not_before_has_passed(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"])
        conn.execute(
            "UPDATE jobs SET not_before = datetime('now', '-1 hour') WHERE id=?", (job_id,)
        )
        conn.commit()

        claimed = jobs.claim_job(conn, job_id, lease_id="lease-1", lease_seconds=60)
        self.assertTrue(claimed)
        conn.close()


class CompleteJobTests(unittest.TestCase):
    def test_completes_with_matching_lease(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"])
        jobs.claim_job(conn, job_id, lease_id="lease-1", lease_seconds=60)

        completed = jobs.complete_job(conn, job_id, lease_id="lease-1", model_version="m1")

        self.assertTrue(completed)
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["model_version"], "m1")
        self.assertIsNotNone(row["completed_at"])
        conn.close()

    def test_rejects_stale_lease(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"])
        jobs.claim_job(conn, job_id, lease_id="lease-1", lease_seconds=60)

        completed = jobs.complete_job(conn, job_id, lease_id="stale-lease")

        self.assertFalse(completed)
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "running", "must not be marked done by a stale lease")
        conn.close()


class FailJobTests(unittest.TestCase):
    def test_retries_when_under_max_attempts(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"], max_attempts=5)
        jobs.claim_job(conn, job_id, lease_id="lease-1", lease_seconds=60)

        outcome = jobs.fail_job(conn, job_id, lease_id="lease-1", error_message="boom")

        self.assertEqual(outcome, "retrying")
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 1)
        self.assertEqual(row["last_error"], "boom")
        self.assertIsNone(row["lease_id"])
        self.assertIsNotNone(row["not_before"])
        self.assertEqual(row["wait_reason"], "error_backoff")
        conn.close()

    def test_terminal_failure_when_max_attempts_reached(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"], max_attempts=1)
        jobs.claim_job(conn, job_id, lease_id="lease-1", lease_seconds=60)

        outcome = jobs.fail_job(conn, job_id, lease_id="lease-1", error_message="boom")

        self.assertEqual(outcome, "failed")
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], 1)
        self.assertIsNotNone(row["completed_at"])
        conn.close()

    def test_terminal_failure_writes_event(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"], max_attempts=1)
        jobs.claim_job(conn, job_id, lease_id="lease-1", lease_seconds=60)

        jobs.fail_job(conn, job_id, lease_id="lease-1", error_message="boom")

        event = conn.execute(
            "SELECT * FROM events WHERE event_type='job_failed_terminal'"
        ).fetchone()
        self.assertIsNotNone(event)
        self.assertEqual(event["job_id"], job_id)
        self.assertEqual(event["actor_type"], "daemon")
        conn.close()

    def test_stale_lease_is_rejected(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"])
        jobs.claim_job(conn, job_id, lease_id="lease-1", lease_seconds=60)

        outcome = jobs.fail_job(conn, job_id, lease_id="stale", error_message="boom")

        self.assertEqual(outcome, "lease_lost")
        row = conn.execute("SELECT attempts FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["attempts"], 0, "a stale-lease failure must not mutate retry state")
        conn.close()


class RecordRateLimitTests(unittest.TestCase):
    def test_stays_pending_and_does_not_touch_attempts(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"])
        jobs.claim_job(conn, job_id, lease_id="lease-1", lease_seconds=60)

        ok = jobs.record_rate_limit(conn, job_id, lease_id="lease-1", retry_after_seconds=30.0)

        self.assertTrue(ok)
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["rate_limit_count"], 1)
        self.assertEqual(row["wait_reason"], "rate_limited")
        self.assertIsNone(row["lease_id"])
        conn.close()

    def test_stale_lease_rejected(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"])
        jobs.claim_job(conn, job_id, lease_id="lease-1", lease_seconds=60)

        ok = jobs.record_rate_limit(conn, job_id, lease_id="stale", retry_after_seconds=30.0)
        self.assertFalse(ok)
        conn.close()


class ReclaimExpiredLeasesTests(unittest.TestCase):
    def test_reclaims_only_expired_leases(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        expired_job = _insert_pending_job(conn, ids["task_id"])
        fresh_job = _insert_pending_job(conn, ids["task_id"])

        jobs.claim_job(conn, expired_job, lease_id="e", lease_seconds=60)
        conn.execute(
            "UPDATE jobs SET lease_expires_at = datetime('now', '-1 hour') WHERE id=?",
            (expired_job,),
        )
        jobs.claim_job(conn, fresh_job, lease_id="f", lease_seconds=3600)
        conn.commit()

        reclaimed = jobs.reclaim_expired_leases(conn)

        self.assertEqual(reclaimed, 1)
        expired_row = conn.execute("SELECT * FROM jobs WHERE id=?", (expired_job,)).fetchone()
        fresh_row = conn.execute("SELECT * FROM jobs WHERE id=?", (fresh_job,)).fetchone()
        self.assertEqual(expired_row["status"], "pending")
        self.assertIsNone(expired_row["lease_id"])
        self.assertEqual(fresh_row["status"], "running")
        conn.close()


if __name__ == "__main__":
    unittest.main()
