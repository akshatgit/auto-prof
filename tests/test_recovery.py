"""Tests for failure classification, recovery policy and failure memory."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import jobs, recovery  # noqa: E402
from autoprof.artifacts import checkpoint_artifact, restore_artifact, write_artifact  # noqa: E402
from tests.helpers import fresh_db, seed_lab_with_student  # noqa: E402


class ClassifyTests(unittest.TestCase):
    """Classified against error strings this system actually emits."""

    CASES = [
        ("codex exec timed out after 900.0s", recovery.WORKER),
        ("no VERDICT line found in review output: ...", recovery.MODEL_OUTPUT),
        ("codex exec produced no output (exited cleanly but wrote nothing)", recovery.MODEL_OUTPUT),
        ("paper output is not an HTML document: sorry", recovery.MODEL_OUTPUT),
        ("unusable decomposition: contained no tasks", recovery.MODEL_OUTPUT),
        ("supervision verdict 'maybe' not one of ('continue',...)", recovery.MODEL_OUTPUT),
        ("Error: maximum context length exceeded", recovery.MODEL_CAPACITY),
        ("rate limit reached, try again in 45s", recovery.MODEL_CAPACITY),
        ("`codex` CLI not found on PATH", recovery.CONFIG),
        ("job is for round 2 but paper 4 is now on round 3", recovery.STATE_CONFLICT),
        ("paper 7 no longer exists", recovery.STATE_CONFLICT),
        # An empty backend response is a model-output failure, not a logic one:
        # the next call may well produce output, so retrying is coherent.
        ("backend returned empty work output; refusing to erase memory", recovery.MODEL_OUTPUT),
        ("something nobody has seen before", recovery.UNKNOWN),
        (None, recovery.UNKNOWN),
    ]

    def test_classification(self):
        for error, expected in self.CASES:
            with self.subTest(error=error):
                self.assertEqual(recovery.classify_failure(error), expected)


class RetryPolicyTests(unittest.TestCase):
    def test_deterministic_failures_are_not_retried(self):
        """Five identical retries of a broken config cost an hour and
        change nothing."""
        for domain in (recovery.CONFIG, recovery.STATE_CONFLICT, recovery.TASK_LOGIC):
            self.assertFalse(recovery.should_retry(domain, attempts=1), domain)

    def test_transient_failures_are_retried(self):
        self.assertTrue(recovery.should_retry(recovery.WORKER, attempts=1))
        self.assertTrue(recovery.should_retry(recovery.MODEL_OUTPUT, attempts=1))

    def test_each_class_has_its_own_budget(self):
        # Malformed output is worth a couple of tries, not five.
        self.assertTrue(recovery.should_retry(recovery.MODEL_OUTPUT, attempts=2))
        self.assertFalse(recovery.should_retry(recovery.MODEL_OUTPUT, attempts=3))

    def test_unknown_retries_briefly_then_stops(self):
        """Retrying is reversible and cheap, so unknown errors get a
        couple of goes -- but escalate rather than burn the full budget."""
        self.assertTrue(recovery.should_retry(recovery.UNKNOWN, attempts=1))
        self.assertFalse(recovery.should_retry(recovery.UNKNOWN, attempts=2))
        self.assertTrue(recovery.lookup(recovery.UNKNOWN).escalate)


class FailJobIntegrationTests(unittest.TestCase):
    def _running_job(self, conn, max_attempts=5):
        ids = seed_lab_with_student(conn)
        cur = conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status, max_attempts) "
            "VALUES ('student_work', 'task', ?, 'pending', ?)",
            (ids["task_id"], max_attempts),
        )
        job_id = cur.lastrowid
        conn.commit()
        jobs.claim_job(conn, job_id, "lease-1", 600)
        return job_id

    def test_config_failure_is_terminal_on_first_attempt(self):
        conn = fresh_db()
        job_id = self._running_job(conn)
        outcome = jobs.fail_job(conn, job_id, "lease-1", "`codex` CLI not found on PATH")
        self.assertEqual(outcome, "failed")
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["attempts"], 1)
        conn.close()

    def test_worker_timeout_retries(self):
        conn = fresh_db()
        job_id = self._running_job(conn)
        outcome = jobs.fail_job(conn, job_id, "lease-1", "codex exec timed out after 900.0s")
        self.assertEqual(outcome, "retrying")
        conn.close()

    def test_terminal_failure_writes_a_failure_memory(self):
        conn = fresh_db()
        job_id = self._running_job(conn)
        jobs.fail_job(conn, job_id, "lease-1", "`codex` CLI not found on PATH")

        row = conn.execute("SELECT * FROM failure_memories").fetchone()
        self.assertEqual(row["classification"], recovery.CONFIG)
        self.assertEqual(row["job_id"], job_id)
        self.assertIn("PATH", row["symptom"])
        self.assertTrue(row["preventive_rule"])
        self.assertEqual(row["resolved"], 0)
        conn.close()

    def test_recurring_failures_are_queryable(self):
        conn = fresh_db()
        for _ in range(2):
            job_id = self._running_job(conn)
            jobs.fail_job(conn, job_id, "lease-1", "`codex` CLI not found on PATH")
        self.assertEqual(len(recovery.recurring_failures(conn, recovery.CONFIG)), 2)
        self.assertEqual(len(recovery.recurring_failures(conn, recovery.WORKER)), 0)
        conn.close()


class VerifyRecoveryTests(unittest.TestCase):
    def test_running_job_fails_the_postcondition(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        cur = conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('student_work', 'task', ?, 'pending')",
            (ids["task_id"],),
        )
        job_id = cur.lastrowid
        conn.commit()
        jobs.claim_job(conn, job_id, "lease-1", 600)

        ok, failed = recovery.verify_recovery(conn, job_id, ("job_not_running", "lease_released"))
        self.assertFalse(ok)
        self.assertEqual(sorted(failed), ["job_not_running", "lease_released"])
        conn.close()

    def test_unknown_check_is_reported_not_silently_passed(self):
        conn = fresh_db()
        ok, failed = recovery.verify_recovery(conn, 1, ("no_such_check",))
        self.assertFalse(ok)
        self.assertIn("unknown_check:no_such_check", failed)
        conn.close()


class CancelJobTests(unittest.TestCase):
    def test_cancel_marks_rather_than_deletes(self):
        """Deleting frees the rowid, and SQLite reuses it -- which let a
        live daemon's writes land on an unrelated job."""
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        cur = conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('student_work', 'task', ?, 'pending')",
            (ids["task_id"],),
        )
        job_id = cur.lastrowid
        conn.commit()

        self.assertTrue(jobs.cancel_job(conn, job_id, "superseded"))
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertIsNotNone(row)  # the row still exists -- id can never be reused
        self.assertEqual(row["status"], "cancelled")
        self.assertIn("superseded", row["last_error"])
        conn.close()

    def test_running_job_is_not_cancellable(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        cur = conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('student_work', 'task', ?, 'pending')",
            (ids["task_id"],),
        )
        job_id = cur.lastrowid
        conn.commit()
        jobs.claim_job(conn, job_id, "lease-1", 600)

        self.assertFalse(jobs.cancel_job(conn, job_id, "too late"))
        self.assertEqual(
            conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0], "running"
        )
        conn.close()

    def test_every_job_gets_a_stable_operation_id(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        opids = set()
        for _ in range(3):
            conn.execute(
                "INSERT INTO jobs (kind, target_type, target_id, status) "
                "VALUES ('student_work', 'task', ?, 'pending')",
                (ids["task_id"],),
            )
        conn.commit()
        for r in conn.execute("SELECT operation_id FROM jobs"):
            self.assertTrue(r["operation_id"])
            opids.add(r["operation_id"])
        self.assertEqual(len(opids), 3)
        conn.close()


class ArtifactCheckpointTests(unittest.TestCase):
    def test_checkpoint_then_restore_recovers_overwritten_memory(self):
        """The exact loss that happened live: empty output overwrote a
        student's accumulated research."""
        with tempfile.TemporaryDirectory() as d:
            memory = Path(d) / "memory.md"
            write_artifact(memory, "hard-won research")

            checkpoint_artifact(memory)
            write_artifact(memory, "")  # the destructive write

            self.assertTrue(restore_artifact(memory))
            self.assertEqual(memory.read_text(), "hard-won research")

    def test_checkpointing_a_missing_file_is_a_noop(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(checkpoint_artifact(Path(d) / "absent.md"))

    def test_restore_reports_when_there_is_nothing_to_restore(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(restore_artifact(Path(d) / "absent.md"))

    def test_history_is_bounded(self):
        with tempfile.TemporaryDirectory() as d:
            memory = Path(d) / "memory.md"
            for i in range(15):
                write_artifact(memory, f"pass {i}")
                checkpoint_artifact(memory, keep=5)
            kept = list((Path(d) / ".checkpoints").glob("memory.md.*"))
            self.assertLessEqual(len(kept), 5)


if __name__ == "__main__":
    unittest.main()
