import tempfile
import sqlite3
import unittest
from types import SimpleNamespace
from pathlib import Path

from autoprof import daemon
from autoprof.backends.base import Backend, BackendResult
from autoprof.runner import PromptSpec
from tests.helpers import fresh_db, seed_lab_with_student


def _drain(timeout=30):
    """Wait for dispatched work to finish.

    Dispatch schedules and returns, so a test that asserts on RESULTS has to
    wait for them. Tests that assert on SCHEDULING should not call this.
    """
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not daemon._reap_inflight():
            return True
        time.sleep(0.05)
    return False


def _dispatch_until_drained(conn, reg, lab_dir, handlers, workers, path, budget=8, ticks=20):
    """Dispatch repeatedly, as the daemon loop does.

    One tick can schedule at most `workers` jobs now that dispatch does not
    block; the rest are picked up by later ticks.
    """
    import time
    total = 0
    for _ in range(ticks):
        total += daemon.dispatch_pending_jobs(
            conn, reg, {}, lab_dir, budget_cap=budget,
            special_handlers=handlers, workers=workers, db_path=path)
        _drain()
        remaining = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0]
        if not remaining:
            break
        time.sleep(0.02)
    return total


def _task_in_new_lab(conn, ids):
    """A task in its own lab.

    One lab admits only one workspace-writing job at a time, so a test
    about dispatch volume needs its jobs spread across labs -- otherwise it
    is measuring the workspace guard, not the budget.
    """
    prof = conn.execute(
        "SELECT professor_id FROM labs WHERE id = ?", (ids["lab_id"],)
    ).fetchone()["professor_id"]
    lab_id = conn.execute(
        "INSERT INTO labs (professor_id, root_problem, status) VALUES (?, 'r', 'active')",
        (prof,),
    ).lastrowid
    return conn.execute(
        "INSERT INTO tasks (lab_id, title, brief_path, direction, end_criteria, status) "
        "VALUES (?, 't', 'b.md', 'open', 'x', 'in_progress')", (lab_id,),
    ).lastrowid


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

    def get_backend(self, kind, reviewer_index=None, lab_id=None):
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
            _insert_pending_job(conn, _task_in_new_lab(conn, ids))
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

    def test_paused_student_work_is_not_dispatched(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"])
        conn.execute(
            "UPDATE students SET paused_at=datetime('now') WHERE id=?",
            (ids["student_id"],),
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
        self.assertEqual(
            conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()["status"],
            "pending",
        )
        conn.close()

    def test_paper_review_backend_receives_owning_lab_id(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        paper_id = conn.execute(
            "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
            "VALUES (?, ?, 'p.html', 'P', 'in_review', 1)",
            (ids["task_id"], ids["student_id"]),
        ).lastrowid
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status, review_round, reviewer_index) "
            "VALUES ('paper_review', 'paper', ?, 'pending', 1, 2)",
            (paper_id,),
        )
        conn.commit()
        seen = []

        class CapturingRegistry:
            def get_backend(self, kind, reviewer_index=None, lab_id=None):
                seen.append((kind, reviewer_index, lab_id))
                return AlwaysOkBackend()

        with tempfile.TemporaryDirectory() as d:
            daemon.dispatch_pending_jobs(
                conn,
                registry=CapturingRegistry(),
                prompt_builders={},
                lab_dir=Path(d),
                budget_cap=1,
                special_handlers={"paper_review": lambda *args: "done"},
            )

        self.assertEqual(seen, [("paper_review", 2, ids["lab_id"])])
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
    def get_backend(self, kind, reviewer_index=None, lab_id=None):
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
            def get_backend(self, kind, reviewer_index=None, lab_id=None):
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
        def get_backend(self, kind, reviewer_index=None, lab_id=None):
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
        second = self._job(conn, {**ids, "task_id": _task_in_new_lab(conn, ids)})
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
            def get_backend(self, kind, reviewer_index=None, lab_id=None):
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
            def get_backend(self, kind, reviewer_index=None, lab_id=None):
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
        def get_backend(self, kind, reviewer_index=None, lab_id=None):
            return SimpleNamespace(name="fake")

    def _db(self, tmp, n_jobs):
        from autoprof import db as db_module
        path = Path(tmp) / "c.db"
        conn = db_module.connect(path)
        db_module.ensure_initialized(conn)
        ids = seed_lab_with_student(conn)
        # One lab now admits only one workspace-writing job at a time, so
        # give each job its own lab: this suite is about the lease protocol
        # under concurrency, and needs jobs that may genuinely run at once.
        prof = conn.execute(
            "SELECT professor_id FROM labs WHERE id = ?", (ids["lab_id"],)
        ).fetchone()["professor_id"]
        for i in range(n_jobs):
            if i == 0:
                task_id = ids["task_id"]
            else:
                lab_id = conn.execute(
                    "INSERT INTO labs (professor_id, root_problem, status) "
                    "VALUES (?, 'r', 'active')", (prof,),
                ).lastrowid
                task_id = conn.execute(
                    "INSERT INTO tasks (lab_id, title, brief_path, direction, "
                    "end_criteria, status) VALUES (?, 't', 'b.md', 'open', 'x', "
                    "'in_progress')", (lab_id,),
                ).lastrowid
            conn.execute(
                "INSERT INTO jobs (kind, target_type, target_id, status) "
                "VALUES ('student_work', 'task', ?, 'pending')",
                (task_id,),
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

            _dispatch_until_drained(
                conn, self._Reg(), Path(tmp), {"student_work": handler}, 4, path)
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
            _drain()   # dispatch schedules; the overlap happens after it returns
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
            _drain()
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


class WorkspaceSerializationTests(unittest.TestCase):
    """One agent at a time may write a lab's shared checkout."""

    def setUp(self):
        self.conn = fresh_db()
        self.ids = seed_lab_with_student(self.conn)
        self.lab_id = self.ids["lab_id"]
        self.task_a = self.ids["task_id"]
        cur = self.conn.execute(
            "INSERT INTO tasks (lab_id, title, brief_path, direction, end_criteria, status) "
            "VALUES (?, 'second', 'b.md', 'open', 'x', 'in_progress')", (self.lab_id,)
        )
        self.task_b = cur.lastrowid
        self.conn.commit()

    def tearDown(self):
        self.conn.close()

    def _job(self, kind, task_id, status="pending"):
        cur = self.conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) VALUES (?, 'task', ?, ?)",
            (kind, task_id, status),
        )
        self.conn.commit()
        return cur.lastrowid

    def _candidates(self):
        return self.conn.execute(
            "SELECT id, kind, reviewer_index, target_type, target_id FROM jobs "
            "WHERE status='pending' ORDER BY id"
        ).fetchall()

    def _kept_ids(self):
        return [r["id"] for r in
                daemon._serialize_workspace_writers(self.conn, self._candidates())]

    def test_only_one_writer_per_lab_per_tick(self):
        a = self._job("student_work", self.task_a)
        self._job("student_work", self.task_b)
        self.assertEqual(self._kept_ids(), [a])

    def test_no_writer_dispatched_while_one_runs(self):
        self._job("student_work", self.task_a, status="running")
        self._job("student_work", self.task_b)
        self.assertEqual(self._kept_ids(), [])

    def test_non_writers_are_never_held_back(self):
        self._job("student_work", self.task_a, status="running")
        sup = self._job("professor_supervision", self.task_b)
        self.assertEqual(self._kept_ids(), [sup])

    def test_revision_and_research_contend_for_the_same_slot(self):
        a = self._job("student_revise_paper", self.task_a)
        self._job("student_work", self.task_b)
        self.assertEqual(self._kept_ids(), [a])

    def test_a_running_paper_targeted_writer_also_holds_the_lab(self):
        # author_response targets a paper, not a task; a task-only join
        # missed it and let two writers into one workspace.
        paper_id = self.conn.execute(
            "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
            "VALUES (?, ?, 'p.html', 'T', 'in_review', 1)",
            (self.task_a, self.ids["student_id"]),
        ).lastrowid
        self.conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('author_response', 'paper', ?, 'running')", (paper_id,),
        )
        self.conn.commit()
        self._job("student_work", self.task_b)
        self.assertEqual(self._kept_ids(), [])

    def test_other_labs_are_unaffected(self):
        prof = self.conn.execute(
            "SELECT professor_id FROM labs WHERE id = ?", (self.lab_id,)
        ).fetchone()["professor_id"]
        other = self.conn.execute(
            "INSERT INTO labs (professor_id, root_problem, status) VALUES (?, 'r', 'active')",
            (prof,),
        ).lastrowid
        other_task = self.conn.execute(
            "INSERT INTO tasks (lab_id, title, brief_path, direction, end_criteria, status) "
            "VALUES (?, 't', 'b.md', 'open', 'x', 'in_progress')", (other,)
        ).lastrowid
        self.conn.commit()
        a = self._job("student_work", self.task_a)
        b = self._job("student_work", other_task)
        self.assertEqual(self._kept_ids(), [a, b])


class NonBlockingDispatchTests(unittest.TestCase):
    """A tick must schedule work, not wait for it."""

    class _Reg:
        def get_backend(self, kind, reviewer_index=None, lab_id=None):
            return SimpleNamespace(name="fake")

    def setUp(self):
        daemon._INFLIGHT.clear()
        daemon._POOL = None
        daemon._POOL_SIZE = 0

    def tearDown(self):
        daemon._INFLIGHT.clear()

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
                (_task_in_new_lab(conn, ids),),
            )
        conn.commit()
        return path, conn

    def test_tick_returns_while_jobs_are_still_running(self):
        """The failure this rules out: one slow job freezing the daemon."""
        import threading, time
        release = threading.Event()

        def handler(conn_, job_id, backend, lab_dir):
            release.wait(timeout=30)
            return "done"

        with tempfile.TemporaryDirectory() as tmp:
            path, conn = self._db(tmp, 2)
            start = time.time()
            n = daemon.dispatch_pending_jobs(
                conn, self._Reg(), {}, Path(tmp), budget_cap=8,
                special_handlers={"student_work": handler},
                workers=4, db_path=path,
            )
            elapsed = time.time() - start
            self.assertEqual(n, 2)
            self.assertLess(elapsed, 5, "dispatch blocked on job completion")
            self.assertEqual(len(daemon._reap_inflight()), 2)
            release.set()

    def test_a_busy_pool_is_not_oversubscribed(self):
        import threading
        release = threading.Event()

        def handler(conn_, job_id, backend, lab_dir):
            release.wait(timeout=30)
            return "done"

        with tempfile.TemporaryDirectory() as tmp:
            path, conn = self._db(tmp, 6)
            first = daemon.dispatch_pending_jobs(
                conn, self._Reg(), {}, Path(tmp), budget_cap=8,
                special_handlers={"student_work": handler}, workers=2, db_path=path)
            second = daemon.dispatch_pending_jobs(
                conn, self._Reg(), {}, Path(tmp), budget_cap=8,
                special_handlers={"student_work": handler}, workers=2, db_path=path)
            self.assertEqual(first, 2)
            self.assertEqual(second, 0, "dispatched past the worker count")
            release.set()

    def test_the_same_job_is_not_submitted_twice(self):
        import threading
        release = threading.Event()

        def handler(conn_, job_id, backend, lab_dir):
            release.wait(timeout=30)
            return "done"

        with tempfile.TemporaryDirectory() as tmp:
            path, conn = self._db(tmp, 1)
            daemon.dispatch_pending_jobs(
                conn, self._Reg(), {}, Path(tmp), budget_cap=8,
                special_handlers={"student_work": handler}, workers=4, db_path=path)
            again = daemon.dispatch_pending_jobs(
                conn, self._Reg(), {}, Path(tmp), budget_cap=8,
                special_handlers={"student_work": handler}, workers=4, db_path=path)
            self.assertEqual(again, 0)
            release.set()

    def test_finished_jobs_free_their_slot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, conn = self._db(tmp, 2)
            daemon.dispatch_pending_jobs(
                conn, self._Reg(), {}, Path(tmp), budget_cap=8,
                special_handlers={"student_work": lambda *a: "done"},
                workers=2, db_path=path)
            import time
            for _ in range(50):
                if not daemon._reap_inflight():
                    break
                time.sleep(0.1)
            self.assertEqual(daemon._reap_inflight(), set())


class InflightGraceTests(unittest.TestCase):
    """A worker that never returns must not hold its slot forever."""

    def setUp(self):
        daemon._INFLIGHT.clear(); daemon._ABANDONED.clear()
        daemon._POOL = None; daemon._POOL_SIZE = 0

    def tearDown(self):
        daemon._INFLIGHT.clear(); daemon._ABANDONED.clear()

    def _never_finishes(self):
        import concurrent.futures
        return concurrent.futures.Future()   # never resolved

    def test_a_fresh_future_is_still_counted(self):
        import time as t
        daemon._INFLIGHT[42] = (self._never_finishes(), t.monotonic())
        self.assertIn(42, daemon._reap_inflight())

    def test_a_stuck_future_is_abandoned_after_the_grace_period(self):
        import time as t
        daemon._INFLIGHT[42] = (self._never_finishes(),
                                t.monotonic() - daemon.INFLIGHT_GRACE_SECONDS - 1)
        self.assertNotIn(42, daemon._reap_inflight())
        self.assertIn(42, daemon._ABANDONED)

    def test_abandoning_frees_the_slot_for_new_work(self):
        import time as t
        for i in range(2):
            daemon._INFLIGHT[i] = (self._never_finishes(),
                                   t.monotonic() - daemon.INFLIGHT_GRACE_SECONDS - 1)
        self.assertEqual(daemon._reap_inflight(), set())

    def test_a_stuck_writer_stops_holding_its_lab(self):
        # The lab-8 symptom: one stuck worker marked the lab busy forever.
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        import time as t
        cur = conn.execute(
            "INSERT INTO jobs (kind,target_type,target_id,status) "
            "VALUES ('student_work','task',?, 'pending')", (ids["task_id"],))
        stuck = cur.lastrowid
        conn.commit()
        daemon._INFLIGHT[stuck] = (self._never_finishes(),
                                   t.monotonic() - daemon.INFLIGHT_GRACE_SECONDS - 1)
        rows = conn.execute(
            "SELECT id,kind,reviewer_index,target_type,target_id FROM jobs "
            "WHERE status='pending'").fetchall()
        kept = daemon._serialize_workspace_writers(conn, rows)
        self.assertTrue(kept, "lab stayed blocked by an abandoned worker")
        conn.close()
