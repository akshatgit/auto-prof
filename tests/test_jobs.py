import unittest

from autoprof import jobs
from autoprof.backends.base import BackendResult
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


class _SessionBackend:
    """Records the opts it was called with and reports a session id."""

    name = "session-fake"

    def __init__(self, session_id="sess-1", **result_kw):
        self.session_id = session_id
        self.result_kw = result_kw
        self.calls = []

    def run(self, prompt, **opts):
        self.calls.append(opts)
        return BackendResult(text="ok", session_id=self.session_id, **self.result_kw)


def _job(conn, kind="student_work"):
    ids = seed_lab_with_student(conn)
    cur = conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status) VALUES (?, 'task', ?, 'running')",
        (kind, ids["task_id"]),
    )
    conn.commit()
    return cur.lastrowid


class RunWithSessionTests(unittest.TestCase):
    def test_first_call_has_no_resume_id_and_records_the_new_one(self):
        conn = fresh_db()
        job_id = _job(conn)
        backend = _SessionBackend("sess-1")

        jobs.run_with_session(conn, job_id, backend, "prompt")

        self.assertNotIn("resume_session_id", backend.calls[0])
        stored = conn.execute(
            "SELECT backend_session_id FROM jobs WHERE id=?", (job_id,)
        ).fetchone()[0]
        self.assertEqual(stored, "sess-1")
        conn.close()

    def test_second_call_resumes_the_recorded_session(self):
        conn = fresh_db()
        job_id = _job(conn)
        backend = _SessionBackend("sess-1")

        jobs.run_with_session(conn, job_id, backend, "prompt")
        jobs.run_with_session(conn, job_id, backend, "prompt")

        self.assertEqual(backend.calls[1]["resume_session_id"], "sess-1")
        conn.close()

    def test_session_is_persisted_even_when_the_call_errored(self):
        """The whole point: a job that died mid-work must still be
        resumable on its next attempt."""
        conn = fresh_db()
        job_id = _job(conn)
        backend = _SessionBackend("sess-9", error="died partway")

        jobs.run_with_session(conn, job_id, backend, "prompt")

        stored = conn.execute(
            "SELECT backend_session_id FROM jobs WHERE id=?", (job_id,)
        ).fetchone()[0]
        self.assertEqual(stored, "sess-9")
        conn.close()

    def test_session_is_persisted_on_token_exhaustion(self):
        conn = fresh_db()
        job_id = _job(conn)
        backend = _SessionBackend("sess-tok", rate_limited=True)

        jobs.run_with_session(conn, job_id, backend, "prompt")

        stored = conn.execute(
            "SELECT backend_session_id FROM jobs WHERE id=?", (job_id,)
        ).fetchone()[0]
        self.assertEqual(stored, "sess-tok")
        conn.close()

    def test_backend_without_sessions_is_tolerated(self):
        """A backend that reports no session_id must not break the job --
        it just always starts fresh."""
        conn = fresh_db()
        job_id = _job(conn)

        class _NoSession:
            name = "no-session"

            def run(self, prompt, **opts):
                return BackendResult(text="ok")

        jobs.run_with_session(conn, job_id, _NoSession(), "prompt")

        stored = conn.execute(
            "SELECT backend_session_id FROM jobs WHERE id=?", (job_id,)
        ).fetchone()[0]
        self.assertIsNone(stored)
        conn.close()

    def test_explicit_resume_id_is_not_overridden(self):
        conn = fresh_db()
        job_id = _job(conn)
        backend = _SessionBackend("sess-1")
        jobs.run_with_session(conn, job_id, backend, "prompt")

        jobs.run_with_session(conn, job_id, backend, "prompt", resume_session_id="caller-choice")

        self.assertEqual(backend.calls[1]["resume_session_id"], "caller-choice")
        conn.close()


class ProviderCircuitBreakerTests(unittest.TestCase):
    """A rate limit is a property of the PROVIDER. Without arming the
    breaker each concurrent worker rediscovers the same limit -- four
    workers, four wasted calls, and the waste scales with concurrency."""

    def _running_job(self, conn):
        ids = seed_lab_with_student(conn)
        cur = conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('student_work', 'task', ?, 'pending')",
            (ids["task_id"],),
        )
        job_id = cur.lastrowid
        conn.commit()
        jobs.claim_job(conn, job_id, "lease-1", 600)
        return job_id

    def test_rate_limit_blocks_the_whole_provider(self):
        conn = fresh_db()
        job_id = self._running_job(conn)
        jobs.record_rate_limit(conn, job_id, "lease-1", 120, provider="codex")

        row = conn.execute("SELECT * FROM provider_state WHERE provider='codex'").fetchone()
        self.assertIsNotNone(row)
        self.assertIsNotNone(row["blocked_until"])
        self.assertIn("rate limited", row["last_signal"])
        conn.close()

    def test_a_longer_block_is_never_shortened_by_a_later_one(self):
        conn = fresh_db()
        jobs.block_provider(conn, "codex", 3600, "long")
        first = conn.execute(
            "SELECT blocked_until FROM provider_state WHERE provider='codex'"
        ).fetchone()[0]
        jobs.block_provider(conn, "codex", 5, "short")
        second = conn.execute(
            "SELECT blocked_until FROM provider_state WHERE provider='codex'"
        ).fetchone()[0]
        self.assertEqual(first, second)
        conn.close()

    def test_omitting_the_provider_leaves_the_breaker_alone(self):
        """Job-level backoff still works without a provider name."""
        conn = fresh_db()
        job_id = self._running_job(conn)
        self.assertTrue(jobs.record_rate_limit(conn, job_id, "lease-1", 60))
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM provider_state").fetchone()[0], 0
        )
        conn.close()

    def test_rate_limit_still_costs_no_attempt(self):
        """A rate limit is not a failure -- §5.3."""
        conn = fresh_db()
        job_id = self._running_job(conn)
        jobs.record_rate_limit(conn, job_id, "lease-1", 60, provider="codex")
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["attempts"], 0)
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["wait_reason"], "rate_limited")
        conn.close()


if __name__ == "__main__":
    unittest.main()
