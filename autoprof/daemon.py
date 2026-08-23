"""The daemon tick loop -- docs/DESIGN.md §5, §5.3 (rate limits/dynamic
wake), §5.4 (connection requirements). §5.2's single-daemon-instance lock
lives here too, since it's what makes the lease protocol's guarantees
actually hold (see runner.py / jobs.py for the lease mechanics
themselves).
"""

import fcntl
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import db as db_module
from . import jobs
from .runner import execute_job


class SingleInstanceLock:
    """OS-level flock so at most one daemon process runs against a given
    autoprof.db at a time -- see docs/DESIGN.md §5.2's explanation of why
    that's what keeps the lease protocol's guarantees real rather than
    theoretical."""

    def __init__(self, lock_path: Path):
        self.lock_path = Path(lock_path)
        self._fh = None

    def acquire(self) -> bool:
        self._fh = open(self.lock_path, "w")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._fh.close()
            self._fh = None
            return False

    def release(self) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(
                f"another autoprof daemon already holds the lock at {self.lock_path}"
            )
        return self

    def __exit__(self, *exc):
        self.release()


def next_wake_delay(
    conn: sqlite3.Connection, default_interval: float, floor: float = 10.0
) -> float:
    """§5.3's dynamic wake-up: min(default heartbeat, nearest per-job
    backoff clearing, nearest provider window reset), clamped to
    [floor, default_interval]."""
    candidates = [default_interval]

    row = conn.execute(
        "SELECT MIN((julianday(not_before) - julianday('now')) * 86400.0) AS secs "
        "FROM jobs WHERE status='pending' AND not_before IS NOT NULL"
    ).fetchone()
    if row["secs"] is not None:
        candidates.append(row["secs"])

    row = conn.execute(
        "SELECT MIN((julianday(blocked_until) - julianday('now')) * 86400.0) AS secs "
        "FROM provider_state WHERE blocked_until IS NOT NULL"
    ).fetchone()
    if row["secs"] is not None:
        candidates.append(row["secs"])

    delay = min(candidates)
    return max(floor, min(delay, default_interval))


def _provider_blocked(conn: sqlite3.Connection, provider: str) -> bool:
    row = conn.execute(
        "SELECT blocked_until FROM provider_state WHERE provider = ?", (provider,)
    ).fetchone()
    if row is None or row["blocked_until"] is None:
        return False
    check = conn.execute(
        "SELECT datetime('now') < ? AS blocked", (row["blocked_until"],)
    ).fetchone()
    return bool(check["blocked"])


def _fail_unhandled(conn: sqlite3.Connection, job_id: int, error: Exception) -> str:
    """Record a handler crash against the job that caused it.

    The handler may have died holding a lease, or before ever claiming
    one, so this cannot go through jobs.fail_job (which requires a
    matching lease). It writes the terminal state directly and leaves the
    error text for diagnosis.
    """
    try:
        conn.rollback()  # discard any partial transaction the handler left
        conn.execute(
            "UPDATE jobs SET status='failed', attempts=attempts+1, last_error=?, "
            "completed_at=datetime('now'), lease_id=NULL, lease_expires_at=NULL WHERE id=?",
            (f"handler raised {type(error).__name__}: {error}"[:2000], job_id),
        )
        conn.commit()
    except Exception:  # noqa: BLE001 -- never let cleanup kill the loop either
        pass
    return "failed"


def _job_lab_id(conn: sqlite3.Connection, row) -> int | None:
    """Resolve the owning lab for backend policy without denormalizing jobs."""
    target_type, target_id = row["target_type"], row["target_id"]
    if target_type == "lab":
        return target_id
    queries = {
        "task": "SELECT lab_id FROM tasks WHERE id=?",
        "paper": (
            "SELECT tasks.lab_id FROM papers JOIN tasks ON tasks.id=papers.task_id "
            "WHERE papers.id=?"
        ),
        "professor": "SELECT lab_id FROM professors WHERE id=?",
        "student": (
            "SELECT professors.lab_id FROM students "
            "JOIN professors ON professors.id=students.professor_id WHERE students.id=?"
        ),
        "defense": (
            "SELECT professors.lab_id FROM defenses "
            "JOIN students ON students.id=defenses.student_id "
            "JOIN professors ON professors.id=students.professor_id WHERE defenses.id=?"
        ),
    }
    query = queries.get(target_type)
    if query is None:
        return None
    owner = conn.execute(query, (target_id,)).fetchone()
    return owner["lab_id"] if owner else None


def _record_backend(conn: sqlite3.Connection, job_id: int, backend) -> None:
    """Note which harness is running a job, as it starts.

    `model_version` is only written when a job COMPLETES, so a running job
    said nothing about what was working on it -- the Jobs view could show
    tokens accumulating with no indication of whether codex, claude or
    ollama produced them.
    """
    model = getattr(backend, "model", None)
    try:
        conn.execute(
            "UPDATE jobs SET backend = ?, backend_model = ? WHERE id = ?",
            (getattr(backend, "name", None), str(model) if model else None, job_id),
        )
        conn.commit()
    except sqlite3.Error:
        pass    # bookkeeping must never stop the job


def _execute_one(
    db_path, job_id: int, kind: str, registry, prompt_builders, lab_dir, special_handlers,
    reviewer_index: int | None = None,
) -> str:
    """Run one job on its own connection.

    Its OWN connection because sqlite3 connections are not safe to share
    across threads. Correctness under concurrency comes from the lease
    protocol, not from locking: claim_job is a single atomic conditional
    UPDATE, so if two workers reach for the same job exactly one wins and
    the loser gets 'not_claimed'.
    """
    conn = db_module.connect(db_path)
    try:
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            backend = registry.get_backend(
                kind, reviewer_index, lab_id=_job_lab_id(conn, row)
            )
        except Exception as e:  # noqa: BLE001
            _fail_unhandled(conn, job_id, e)
            return "failed"

        if _provider_blocked(conn, backend.name):
            return "not_claimed"

        _record_backend(conn, job_id, backend)

        handler = special_handlers.get(kind)
        if handler is not None:
            try:
                return handler(conn, job_id, backend, lab_dir)
            except Exception as e:  # noqa: BLE001
                return _fail_unhandled(conn, job_id, e)
        return execute_job(conn, job_id, backend, prompt_builders, lab_dir)
    finally:
        conn.close()


# Job kinds whose handler lets an agentic backend write the lab workspace.
# Two of these running at once in one lab means two agents editing one
# checkout, and `git add -A` at the end of each sweeps up whatever the other
# had half-written.
WORKSPACE_WRITER_KINDS = frozenset(
    {"student_work", "student_revise_paper", "author_response"}
)


# Jobs handed to the pool but not yet finished. The tick used to block on
# `pool.map` until every job it dispatched completed, so one long research
# round froze the whole daemon: six workers idle, four jobs pending, one
# running, and no tick for four minutes. Reclaim could not fire either,
# because it runs at the top of a tick and the tick was the thing stuck.
_INFLIGHT: dict = {}
# Job ids whose worker never returned. Kept only so the condition is
# reportable rather than silent.
_ABANDONED: set = set()
_INFLIGHT_LOCK = threading.Lock()
_POOL = None
_POOL_SIZE = 0


def _worker_pool(workers: int):
    """One long-lived pool. A per-tick pool cannot outlive its tick."""
    global _POOL, _POOL_SIZE
    with _INFLIGHT_LOCK:
        if _POOL is None or _POOL_SIZE != workers:
            _POOL = ThreadPoolExecutor(max_workers=workers,
                                       thread_name_prefix="autoprof-worker")
            _POOL_SIZE = workers
        return _POOL


# How long a future may stay in flight before dispatch stops counting it.
# A worker that never returns would otherwise hold its slot AND mark its lab
# busy forever: lab 8 sat idle for 350 ticks behind one such thread while five
# workers were free. Matching the lease means the job is reclaimable at the
# same moment we stop waiting for it.
INFLIGHT_GRACE_SECONDS = 1800


def _reap_inflight() -> set:
    """Drop finished futures; return the job ids still considered executing.

    Abandoning a stuck entry does NOT kill its thread -- we cannot -- but it
    frees the slot and lets the lease protocol reclaim the job, which is the
    same escape a crashed daemon gets.
    """
    now = time.monotonic()
    with _INFLIGHT_LOCK:
        for job_id, (future, started) in list(_INFLIGHT.items()):
            if future.done():
                _INFLIGHT.pop(job_id)
                try:
                    future.result()
                except Exception:   # noqa: BLE001 -- already recorded on the job row
                    pass
            elif now - started > INFLIGHT_GRACE_SECONDS:
                _INFLIGHT.pop(job_id)
                _ABANDONED.add(job_id)
        return set(_INFLIGHT)


def _serialize_workspace_writers(conn: sqlite3.Connection, candidates: list) -> list:
    """At most one workspace-writing job per lab, and none while one runs.

    Lab 9 ran tasks 34, 35 and 36 concurrently against a single checkout.
    The result: 139 files belonging to two tasks landed in a third task's
    tree, a paper-revision commit bundled another student's unfinished
    work, and the ecosystem-exposure student modified the cache-soundness
    reducer that a different task's paper depends on. Provenance is the
    thing reviewers attack hardest, and this was quietly destroying it.

    Serialising rather than isolating is deliberate: tasks 35 and 36 are
    specified in terms of task 34's tool, so they must share the checkout.
    They just must not write it at the same time.
    """
    # Resolve through _job_lab_id rather than a join on tasks: author_response
    # targets a PAPER, so a task-only join silently reports no owning lab and
    # lets a paper-targeted writer run alongside a task-targeted one.
    running = conn.execute(
        "SELECT target_type, target_id FROM jobs WHERE status = 'running' "
        "AND kind IN ({})".format(",".join("?" * len(WORKSPACE_WRITER_KINDS))),
        tuple(sorted(WORKSPACE_WRITER_KINDS)),
    ).fetchall()
    busy = set()
    for row in running:
        lab_id = _job_lab_id(conn, row)
        if lab_id is not None:
            busy.add(lab_id)
    # A job just submitted has not necessarily claimed itself yet, so its lab
    # would still look free for one tick and admit a second writer.
    for job_id in _reap_inflight():
        row = conn.execute(
            "SELECT kind, target_type, target_id FROM jobs WHERE id = ?", (job_id,)
        ).fetchone()
        if row is not None and row["kind"] in WORKSPACE_WRITER_KINDS:
            lab_id = _job_lab_id(conn, row)
            if lab_id is not None:
                busy.add(lab_id)

    kept = []
    for candidate in candidates:
        if candidate["kind"] not in WORKSPACE_WRITER_KINDS:
            kept.append(candidate)
            continue
        lab_id = _job_lab_id(conn, candidate)
        if lab_id is None:
            kept.append(candidate)
            continue
        if lab_id in busy:
            continue          # another writer holds this lab's workspace
        busy.add(lab_id)      # and this one now claims it for the tick
        kept.append(candidate)
    return kept


def dispatch_pending_jobs(
    conn: sqlite3.Connection,
    registry,
    prompt_builders: dict,
    lab_dir: Path,
    budget_cap: int,
    special_handlers: dict | None = None,
    workers: int = 1,
    db_path=None,
) -> int:
    """Dispatch up to `budget_cap` eligible pending jobs this tick.
    Provider-blocked jobs and jobs skipped for any other reason don't
    count against the cap; only actually-attempted jobs do.

    `special_handlers` maps a job kind to `handler(conn, job_id, backend,
    lab_dir) -> outcome`, taking precedence over the generic
    prompt-builder path (runner.execute_job) for that kind -- e.g.
    lab_review needs to parse a verdict and tally reviewers, which a
    single PromptSpec artifact write can't express (see
    autoprof/lab_review.py)."""
    special_handlers = special_handlers or {}
    dispatched = 0
    # Ordered by attempts first, then age. Strict FIFO let one repeatedly
    # failing job hold a budget slot every tick forever: observed live as a
    # student_work job that consumed a slot on each of three attempts while
    # six ready paper_review jobs behind it never started once. Preferring
    # untried work means a struggling job still makes progress -- it just
    # yields to jobs that have not had their turn yet.
    candidate_rows = conn.execute(
        "SELECT id, kind, reviewer_index, target_type, target_id "
        "FROM jobs WHERE status='pending' "
        "AND (not_before IS NULL OR not_before <= datetime('now')) "
        "AND NOT (kind='student_work' AND EXISTS ("
        "SELECT 1 FROM students s WHERE s.task_id=jobs.target_id "
        "AND s.paused_at IS NOT NULL)) "
        "ORDER BY attempts, created_at LIMIT ?",
        (max(budget_cap * 4, budget_cap),),
    ).fetchall()
    candidate_rows = _serialize_workspace_writers(conn, candidate_rows)

    if workers > 1:
        if db_path is None:
            raise ValueError("concurrent dispatch needs db_path so each worker can connect")
        pool = _worker_pool(workers)
        inflight = _reap_inflight()
        free = workers - len(inflight)
        if free <= 0:
            return 0
        chosen = [row for row in candidate_rows if row["id"] not in inflight][:min(budget_cap, free)]
        if not chosen:
            return 0
        with _INFLIGHT_LOCK:
            for row in chosen:
                future = pool.submit(
                    _execute_one,
                    db_path, row["id"], row["kind"], registry,
                    prompt_builders, lab_dir, special_handlers,
                    row["reviewer_index"],
                )
                _INFLIGHT[row["id"]] = (future, time.monotonic())
        return len(chosen)

    for candidate in candidate_rows:
        if dispatched >= budget_cap:
            break

        # Resolving a backend can fail on its own -- an unknown job kind
        # raises here, BEFORE the handler guard below. A daemon running
        # code older than the kind it is dispatching hits exactly this,
        # and it used to take the whole loop down with it.
        try:
            backend = registry.get_backend(
                candidate["kind"], candidate["reviewer_index"],
                lab_id=_job_lab_id(conn, candidate),
            )
        except Exception as e:  # noqa: BLE001 -- one job's problem, not the loop's
            _fail_unhandled(conn, candidate["id"], e)
            dispatched += 1
            continue

        if _provider_blocked(conn, backend.name):
            continue

        _record_backend(conn, candidate["id"], backend)

        handler = special_handlers.get(candidate["kind"])
        if handler is not None:
            # A handler that raises must fail ITS OWN job, never the loop.
            # Without this a single unhandled exception took the daemon
            # down and every lab stopped -- observed live, from a NOT NULL
            # violation in one event write. runner.execute_job already had
            # this guard; the special-handler path did not.
            try:
                outcome = handler(conn, candidate["id"], backend, lab_dir)
            except Exception as e:  # noqa: BLE001 -- see above
                outcome = _fail_unhandled(conn, candidate["id"], e)
        else:
            outcome = execute_job(conn, candidate["id"], backend, prompt_builders, lab_dir)

        if outcome != "not_claimed":
            dispatched += 1

    return dispatched


def run_tick(
    conn: sqlite3.Connection,
    registry,
    prompt_builders: dict,
    lab_dir: Path,
    budget_cap: int,
    special_handlers: dict | None = None,
    workers: int = 1,
    db_path=None,
) -> dict:
    reclaimed = jobs.reclaim_expired_leases(conn)
    # A review job that exhausts its retries leaves its paper in_review with
    # nothing left to tally it. Recover those before dispatching.
    from .paper_review import sweep_stalled_reviews
    sweep_stalled_reviews(conn)
    dispatched = dispatch_pending_jobs(
        conn, registry, prompt_builders, lab_dir, budget_cap, special_handlers,
        workers=workers, db_path=db_path,
    )
    return {"reclaimed": reclaimed, "dispatched": dispatched}


MAX_CONSECUTIVE_TICK_FAILURES = 10
TICK_FAILURE_BACKOFF_SECONDS = 15.0
MAX_TICK_FAILURE_BACKOFF_SECONDS = 120.0


def run_daemon(
    conn: sqlite3.Connection,
    registry,
    prompt_builders: dict,
    lab_dir: Path,
    budget_cap: int = 10,
    default_interval: float = 300.0,
    once: bool = False,
    sleep_fn=time.sleep,
    max_ticks: int | None = None,
    special_handlers: dict | None = None,
    on_tick=None,
    workers: int = 1,
    db_path=None,
) -> None:
    """The tick loop from docs/DESIGN.md §5. `once=True` runs a single
    tick and returns (used for `autoprof daemon run --once` and for
    tests); otherwise loops until `max_ticks` is reached or forever.

    `on_tick(tick_number, stats, delay)` is called after each tick with
    what that tick did and how long the daemon is about to sleep. An
    unattended daemon is otherwise completely silent for hours at a time,
    which makes "working through a queue slowly" and "wedged" look
    identical from outside; `delay` is None on the final tick, when
    there's no sleep left to report.
    """
    ticks = 0
    consecutive_failures = 0
    while True:
        try:
            stats = run_tick(
                conn, registry, prompt_builders, lab_dir, budget_cap, special_handlers,
                workers=workers, db_path=db_path,
            )
        except sqlite3.OperationalError as exc:
            # A momentary "database is locked" once killed the daemon
            # outright and eight hours of research sat idle behind it. One
            # bad tick is not a reason to stop; a run of them is.
            consecutive_failures += 1
            stats = {"reclaimed": 0, "dispatched": 0, "error": str(exc)}
            if consecutive_failures >= MAX_CONSECUTIVE_TICK_FAILURES:
                raise
            # Retrying immediately just spends the whole allowance inside the
            # same lock: ten ticks at the poll interval burned through it in
            # under two minutes while the writer holding the database had not
            # even finished. Back off so the allowance covers a real outage.
            sleep_fn(min(TICK_FAILURE_BACKOFF_SECONDS * consecutive_failures,
                         MAX_TICK_FAILURE_BACKOFF_SECONDS))
        else:
            consecutive_failures = 0
        ticks += 1

        last = once or (max_ticks is not None and ticks >= max_ticks)
        if last:
            delay = None
        else:
            # Scheduling and reporting read the database too, so they fail
            # the same way a tick does. Fall back to the fixed interval
            # rather than losing the loop to a query that is only advisory.
            try:
                delay = next_wake_delay(conn, default_interval)
            except sqlite3.OperationalError:
                delay = default_interval
        if on_tick is not None:
            try:
                on_tick(ticks, stats, delay)
            except sqlite3.OperationalError:
                pass
        if last:
            return
        sleep_fn(delay)
