import tempfile
import sqlite3
import unittest
from types import SimpleNamespace
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


class OnTickCallbackTests(unittest.TestCase):
    def test_on_tick_receives_stats_and_delay_per_tick(self):
        conn = fresh_db()
        seen = []
        daemon.run_daemon(
            conn,
            registry=_NullRegistry(),
            prompt_builders={},
            lab_dir=Path("/tmp"),
            max_ticks=2,
            sleep_fn=lambda _s: None,
            on_tick=lambda tick, stats, delay: seen.append((tick, stats, delay)),
        )
        self.assertEqual([t for t, _, _ in seen], [1, 2])
        self.assertEqual(seen[0][1], {"reclaimed": 0, "dispatched": 0})
        # A sleep is coming after tick 1 but not after the final tick.
        self.assertIsNotNone(seen[0][2])
        self.assertIsNone(seen[1][2])
        conn.close()

    def test_once_reports_a_single_tick_with_no_delay(self):
        conn = fresh_db()
        seen = []
        daemon.run_daemon(
            conn,
            registry=_NullRegistry(),
            prompt_builders={},
            lab_dir=Path("/tmp"),
            once=True,
            sleep_fn=lambda _s: None,
            on_tick=lambda tick, stats, delay: seen.append((tick, stats, delay)),
        )
        self.assertEqual(len(seen), 1)
        self.assertIsNone(seen[0][2])
        conn.close()


class _NullRegistry:
    def get_backend(self, kind):
        raise AssertionError("no jobs should be dispatched in these tests")


class DispatchOrderingTests(unittest.TestCase):
    def test_untried_jobs_are_preferred_over_repeatedly_failed_ones(self):
        """A job that keeps failing must not starve ready work behind it."""
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        old_failing = conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status, attempts, created_at) "
            "VALUES ('student_work', 'task', ?, 'pending', 3, '2020-01-01 00:00:00')",
            (ids["task_id"],),
        ).lastrowid
        newer_fresh = conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status, attempts, created_at) "
            "VALUES ('student_work', 'task', ?, 'pending', 0, '2030-01-01 00:00:00')",
            (ids["task_id"],),
        ).lastrowid
        conn.commit()

        dispatched = []

        class _Reg:
            def get_backend(self, kind):
                return SimpleNamespace(name="fake")

        def handler(conn_, job_id, backend, lab_dir):
            dispatched.append(job_id)
            return "done"

        daemon.dispatch_pending_jobs(
            conn, _Reg(), {}, Path("/tmp"), budget_cap=1,
            special_handlers={"student_work": handler},
        )

        self.assertEqual(dispatched, [newer_fresh])
        self.assertNotIn(old_failing, dispatched)
        conn.close()


class HandlerCrashTests(unittest.TestCase):
    """One handler raising must not stop the daemon -- it did once, and
    every lab halted until a human noticed."""

    class _Reg:
        def get_backend(self, kind):
            return SimpleNamespace(name="fake")

    def _job(self, conn, ids):
        cur = conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('student_work', 'task', ?, 'pending')",
            (ids["task_id"],),
        )
        conn.commit()
        return cur.lastrowid

    def test_crash_fails_the_job_not_the_loop(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = self._job(conn, ids)

        def exploding(conn_, job, backend, lab_dir):
            raise sqlite3.IntegrityError("NOT NULL constraint failed: events.target_id")

        dispatched = daemon.dispatch_pending_jobs(
            conn, self._Reg(), {}, Path("/tmp"), budget_cap=2,
            special_handlers={"student_work": exploding},
        )

        self.assertEqual(dispatched, 1)  # the loop kept going
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("IntegrityError", row["last_error"])
        conn.close()

    def test_later_jobs_still_run_after_an_earlier_crash(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        first = self._job(conn, ids)
        second = self._job(conn, ids)
        seen = []

        def handler(conn_, job, backend, lab_dir):
            if job == first:
                raise RuntimeError("boom")
            seen.append(job)
            return "done"

        daemon.dispatch_pending_jobs(
            conn, self._Reg(), {}, Path("/tmp"), budget_cap=5,
            special_handlers={"student_work": handler},
        )
        self.assertEqual(seen, [second])
        conn.close()


class UnknownKindTests(unittest.TestCase):
    """A daemon running code older than the job kind it is dispatching
    must fail that job, not stop serving every lab."""

    def test_unresolvable_backend_fails_only_that_job(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        cur = conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('a_kind_from_the_future', 'task', ?, 'pending')",
            (ids["task_id"],),
        )
        job_id = cur.lastrowid
        conn.commit()

        class _Reg:
            def get_backend(self, kind):
                raise ValueError(f"unknown job kind: {kind!r}")

        dispatched = daemon.dispatch_pending_jobs(
            conn, _Reg(), {}, Path("/tmp"), budget_cap=2, special_handlers={},
        )
        self.assertEqual(dispatched, 1)
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertIn("unknown job kind", row["last_error"])
        conn.close()

    def test_a_known_job_after_an_unknown_one_still_runs(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('a_kind_from_the_future', 'task', ?, 'pending')", (ids["task_id"],))
        cur = conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('student_work', 'task', ?, 'pending')", (ids["task_id"],))
        good = cur.lastrowid
        conn.commit()
        seen = []

        class _Reg:
            def get_backend(self, kind):
                if kind == "student_work":
                    return SimpleNamespace(name="fake")
                raise ValueError(f"unknown job kind: {kind!r}")

        daemon.dispatch_pending_jobs(
            conn, _Reg(), {}, Path("/tmp"), budget_cap=5,
            special_handlers={"student_work": lambda c, j, b, d: seen.append(j) or "done"},
        )
        self.assertEqual(seen, [good])
        conn.close()


class ConcurrentDispatchTests(unittest.TestCase):
    """Correctness under concurrency comes from the lease protocol, not
    from locking: claim_job is one atomic conditional UPDATE."""

    class _Reg:
        def get_backend(self, kind):
            return SimpleNamespace(name="fake")

    def _db(self, tmp, n_jobs):
        from autoprof import db as db_module
        path = Path(tmp) / "c.db"
        conn = db_module.connect(path)
        db_module.ensure_initialized(conn)
        ids = seed_lab_with_student(conn)
        for _ in range(n_jobs):
            conn.execute(
                "INSERT INTO jobs (kind, target_type, target_id, status) "
                "VALUES ('student_work', 'task', ?, 'pending')",
                (ids["task_id"],),
            )
        conn.commit()
        return path, conn

    def test_each_job_is_claimed_exactly_once(self):
        """The failure this must rule out: two workers both running one
        job and both writing its result."""
        import threading

        with tempfile.TemporaryDirectory() as tmp:
            path, conn = self._db(tmp, 8)
            seen, lock = [], threading.Lock()

            def handler(conn_, job_id, backend, lab_dir):
                from autoprof import jobs as jobs_module
                lease = f"lease-{job_id}-{threading.get_ident()}"
                if not jobs_module.claim_job(conn_, job_id, lease, 600):
                    return "not_claimed"
                with lock:
                    seen.append(job_id)
                jobs_module.complete_job(conn_, job_id, lease)
                return "done"

            daemon.dispatch_pending_jobs(
                conn, self._Reg(), {}, Path(tmp), budget_cap=8,
                special_handlers={"student_work": handler},
                workers=4, db_path=path,
            )
            self.assertEqual(len(seen), len(set(seen)), "a job ran twice")
            self.assertEqual(len(seen), 8)
            conn.close()

    def test_work_actually_overlaps(self):
        """Otherwise this is just a slower serial loop."""
        import threading
        import time as _time

        with tempfile.TemporaryDirectory() as tmp:
            path, conn = self._db(tmp, 4)
            active, peak, lock = 0, [0], threading.Lock()

            def handler(conn_, job_id, backend, lab_dir):
                nonlocal active
                with lock:
                    active += 1
                    peak[0] = max(peak[0], active)
                _time.sleep(0.25)
                with lock:
                    active -= 1
                return "done"

            daemon.dispatch_pending_jobs(
                conn, self._Reg(), {}, Path(tmp), budget_cap=4,
                special_handlers={"student_work": handler},
                workers=4, db_path=path,
            )
            self.assertGreater(peak[0], 1, "jobs did not run concurrently")
            conn.close()

    def test_one_worker_crashing_does_not_stop_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, conn = self._db(tmp, 4)
            done = []

            def handler(conn_, job_id, backend, lab_dir):
                if job_id % 2 == 0:
                    raise RuntimeError("worker exploded")
                done.append(job_id)
                return "done"

            daemon.dispatch_pending_jobs(
                conn, self._Reg(), {}, Path(tmp), budget_cap=4,
                special_handlers={"student_work": handler},
                workers=4, db_path=path,
            )
            self.assertTrue(done)
            failed = conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status='failed'"
            ).fetchone()[0]
            self.assertGreater(failed, 0)
            conn.close()

    def test_concurrent_dispatch_requires_a_db_path(self):
        """Each worker needs its own connection; a shared one would be
        used across threads."""
        conn = fresh_db()
        with self.assertRaises(ValueError):
            daemon.dispatch_pending_jobs(
                conn, self._Reg(), {}, Path("/tmp"), budget_cap=2, workers=4, db_path=None,
            )
        conn.close()

    def test_single_worker_keeps_the_serial_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, conn = self._db(tmp, 2)
            order = []
            daemon.dispatch_pending_jobs(
                conn, self._Reg(), {}, Path(tmp), budget_cap=2,
                special_handlers={
                    "student_work": lambda c, j, b, d: order.append(j) or "done"
                },
                workers=1,
            )
            self.assertEqual(len(order), 2)
            conn.close()


if __name__ == "__main__":
    unittest.main()
