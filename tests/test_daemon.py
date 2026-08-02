import tempfile
import unittest
from pathlib import Path

from autoprof import daemon
from autoprof.backends.base import Backend, BackendResult
from autoprof.runner import PromptSpec
from tests.helpers import fresh_db, seed_lab_with_student


def _insert_pending_job(conn, task_id, kind="student_work"):
    cur = conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status) VALUES (?, 'task', ?, 'pending')",
        (kind, task_id),
    )
    conn.commit()
    return cur.lastrowid


class AlwaysOkBackend(Backend):
    name = "always_ok"

    def __init__(self):
        self.calls = 0

    def run(self, prompt, **opts):
        self.calls += 1
        return BackendResult(text="ok")


def _builder(conn, row):
    return PromptSpec(prompt="p", artifact_relpath=None, event_type="x", actor_type="student", actor_id=None)


class FakeRegistry:
    def __init__(self, backend):
        self.backend = backend

    def get_backend(self, kind):
        return self.backend


class NextWakeDelayTests(unittest.TestCase):
    def test_defaults_to_interval_when_nothing_pending(self):
        conn = fresh_db()
        delay = daemon.next_wake_delay(conn, default_interval=300, floor=10)
        self.assertEqual(delay, 300)
        conn.close()

    def test_shrinks_to_nearest_not_before(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"])
        conn.execute("UPDATE jobs SET not_before = datetime('now', '+60 seconds') WHERE id=?", (job_id,))
        conn.commit()

        delay = daemon.next_wake_delay(conn, default_interval=300, floor=10)
        self.assertLess(delay, 300)
        self.assertGreater(delay, 0)
        conn.close()

    def test_shrinks_to_nearest_provider_block(self):
        conn = fresh_db()
        conn.execute(
            "INSERT INTO provider_state (provider, blocked_until) VALUES ('codex', datetime('now', '+45 seconds'))"
        )
        conn.commit()
        delay = daemon.next_wake_delay(conn, default_interval=300, floor=10)
        self.assertLess(delay, 300)
        conn.close()

    def test_never_below_floor(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"])
        conn.execute("UPDATE jobs SET not_before = datetime('now', '-10 seconds') WHERE id=?", (job_id,))
        conn.commit()
        delay = daemon.next_wake_delay(conn, default_interval=300, floor=10)
        self.assertGreaterEqual(delay, 10)
        conn.close()

    def test_never_above_default_interval(self):
        conn = fresh_db()
        conn.execute(
            "INSERT INTO provider_state (provider, blocked_until) VALUES ('codex', datetime('now', '+10 hours'))"
        )
        conn.commit()
        delay = daemon.next_wake_delay(conn, default_interval=300, floor=10)
        self.assertLessEqual(delay, 300)
        conn.close()


class DispatchPendingJobsTests(unittest.TestCase):
    def test_dispatches_up_to_budget_cap(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        for _ in range(5):
            _insert_pending_job(conn, ids["task_id"])
        backend = AlwaysOkBackend()

        with tempfile.TemporaryDirectory() as d:
            dispatched = daemon.dispatch_pending_jobs(
                conn,
                registry=FakeRegistry(backend),
                prompt_builders={"student_work": _builder},
                lab_dir=Path(d),
                budget_cap=3,
            )

        self.assertEqual(dispatched, 3)
        self.assertEqual(backend.calls, 3)
        remaining_pending = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status='pending'"
        ).fetchone()["n"]
        self.assertEqual(remaining_pending, 2)
        conn.close()

    def test_skips_jobs_whose_provider_is_blocked(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        _insert_pending_job(conn, ids["task_id"])
        conn.execute(
            "INSERT INTO provider_state (provider, blocked_until) VALUES (?, datetime('now', '+1 hour'))",
            (AlwaysOkBackend.name,),
        )
        conn.commit()
        backend = AlwaysOkBackend()

        with tempfile.TemporaryDirectory() as d:
            dispatched = daemon.dispatch_pending_jobs(
                conn, registry=FakeRegistry(backend),
                prompt_builders={"student_work": _builder}, lab_dir=Path(d), budget_cap=10,
            )

        self.assertEqual(dispatched, 0)
        self.assertEqual(backend.calls, 0)
        row = conn.execute("SELECT status FROM jobs").fetchone()
        self.assertEqual(row["status"], "pending")
        conn.close()

    def test_ignores_jobs_not_yet_eligible(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"])
        conn.execute("UPDATE jobs SET not_before = datetime('now', '+1 hour') WHERE id=?", (job_id,))
        conn.commit()
        backend = AlwaysOkBackend()

        with tempfile.TemporaryDirectory() as d:
            dispatched = daemon.dispatch_pending_jobs(
                conn, registry=FakeRegistry(backend),
                prompt_builders={"student_work": _builder}, lab_dir=Path(d), budget_cap=10,
            )
        self.assertEqual(dispatched, 0)
        conn.close()


class SpecialHandlersTests(unittest.TestCase):
    def test_special_handler_takes_precedence_over_generic_path(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"], kind="lab_review")
        backend = AlwaysOkBackend()
        calls = []

        def special(conn, job_id, backend, lab_dir):
            calls.append(job_id)
            return "done"

        with tempfile.TemporaryDirectory() as d:
            dispatched = daemon.dispatch_pending_jobs(
                conn, registry=FakeRegistry(backend),
                prompt_builders={}, lab_dir=Path(d), budget_cap=10,
                special_handlers={"lab_review": special},
            )

        self.assertEqual(dispatched, 1)
        self.assertEqual(calls, [job_id])
        self.assertEqual(backend.calls, 0, "the generic path must not also run")


class SingleInstanceLockTests(unittest.TestCase):
    def test_second_acquire_fails_while_first_holds_it(self):
        with tempfile.TemporaryDirectory() as d:
            lock_path = Path(d) / "autoprof.lock"
            lock1 = daemon.SingleInstanceLock(lock_path)
            lock2 = daemon.SingleInstanceLock(lock_path)

            self.assertTrue(lock1.acquire())
            self.assertFalse(lock2.acquire())

            lock1.release()
            self.assertTrue(lock2.acquire())
            lock2.release()


class RunTickTests(unittest.TestCase):
    def test_reclaims_and_dispatches_in_one_tick(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        stuck_job = _insert_pending_job(conn, ids["task_id"])
        conn.execute(
            "UPDATE jobs SET status='running', lease_id='x', lease_expires_at=datetime('now', '-1 hour') WHERE id=?",
            (stuck_job,),
        )
        conn.commit()
        backend = AlwaysOkBackend()

        with tempfile.TemporaryDirectory() as d:
            summary = daemon.run_tick(
                conn, registry=FakeRegistry(backend),
                prompt_builders={"student_work": _builder}, lab_dir=Path(d), budget_cap=10,
            )

        self.assertEqual(summary["reclaimed"], 1)
        self.assertEqual(summary["dispatched"], 1)
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (stuck_job,)).fetchone()
        self.assertEqual(row["status"], "done")
        conn.close()


class RunDaemonOnceTests(unittest.TestCase):
    def test_once_runs_exactly_one_tick_and_returns(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        _insert_pending_job(conn, ids["task_id"])
        backend = AlwaysOkBackend()
        sleep_calls = []

        with tempfile.TemporaryDirectory() as d:
            daemon.run_daemon(
                conn, registry=FakeRegistry(backend),
                prompt_builders={"student_work": _builder}, lab_dir=Path(d),
                budget_cap=10, default_interval=300, once=True,
                sleep_fn=sleep_calls.append,
            )

        self.assertEqual(backend.calls, 1)
        self.assertEqual(sleep_calls, [], "once=True must return without sleeping")
        conn.close()


if __name__ == "__main__":
    unittest.main()
