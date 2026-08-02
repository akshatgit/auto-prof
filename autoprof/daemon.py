"""The daemon tick loop -- docs/DESIGN.md §5, §5.3 (rate limits/dynamic
wake), §5.4 (connection requirements). §5.2's single-daemon-instance lock
lives here too, since it's what makes the lease protocol's guarantees
actually hold (see runner.py / jobs.py for the lease mechanics
themselves).
"""

import fcntl
import sqlite3
import time
from pathlib import Path

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


def dispatch_pending_jobs(
    conn: sqlite3.Connection,
    registry,
    prompt_builders: dict,
    lab_dir: Path,
    budget_cap: int,
    special_handlers: dict | None = None,
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
    candidate_rows = conn.execute(
        "SELECT id, kind FROM jobs WHERE status='pending' "
        "AND (not_before IS NULL OR not_before <= datetime('now')) "
        "ORDER BY created_at LIMIT ?",
        (max(budget_cap * 4, budget_cap),),
    ).fetchall()

    for candidate in candidate_rows:
        if dispatched >= budget_cap:
            break

        backend = registry.get_backend(candidate["kind"])
        if _provider_blocked(conn, backend.name):
            continue

        handler = special_handlers.get(candidate["kind"])
        if handler is not None:
            outcome = handler(conn, candidate["id"], backend, lab_dir)
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
) -> dict:
    reclaimed = jobs.reclaim_expired_leases(conn)
    dispatched = dispatch_pending_jobs(
        conn, registry, prompt_builders, lab_dir, budget_cap, special_handlers
    )
    return {"reclaimed": reclaimed, "dispatched": dispatched}


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
    while True:
        stats = run_tick(conn, registry, prompt_builders, lab_dir, budget_cap, special_handlers)
        ticks += 1

        last = once or (max_ticks is not None and ticks >= max_ticks)
        delay = None if last else next_wake_delay(conn, default_interval)
        if on_tick is not None:
            on_tick(ticks, stats, delay)
        if last:
            return
        sleep_fn(delay)
