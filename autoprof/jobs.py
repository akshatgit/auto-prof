"""Job lease/retry state machine -- docs/DESIGN.md §5.1/§5.2/§5.3.

Pure DB state transitions: no backend calls happen here (see
autoprof/runner.py for the piece that actually dispatches to a Backend).
Kept separate so this state machine is testable without ever touching a
subprocess or the network.
"""

import sqlite3

from . import recovery
from .events import record_job_event

MAX_ERROR_BACKOFF_SECONDS = 3600
MAX_RATE_LIMIT_BACKOFF_SECONDS = 3600
_ERROR_BACKOFF_BASE_SECONDS = 30
_RATE_LIMIT_BACKOFF_BASE_SECONDS = 60


def compute_error_backoff_seconds(attempts: int) -> float:
    """Exponential backoff for genuine execution failures -- §5.1."""
    return min(_ERROR_BACKOFF_BASE_SECONDS * (2 ** (attempts - 1)), MAX_ERROR_BACKOFF_SECONDS)


def compute_rate_limit_backoff_seconds(rate_limit_count: int, explicit_seconds: float | None) -> float:
    """§5.3: prefer the backend's own retry-after signal; otherwise
    exponential backoff keyed on rate_limit_count, never on attempts."""
    if explicit_seconds is not None:
        return explicit_seconds
    return min(
        _RATE_LIMIT_BACKOFF_BASE_SECONDS * (2 ** (rate_limit_count - 1)),
        MAX_RATE_LIMIT_BACKOFF_SECONDS,
    )


def claim_job(conn: sqlite3.Connection, job_id: int, lease_id: str, lease_seconds: int) -> bool:
    """Atomically claim a pending, eligible job. §5.2's lease protocol."""
    cur = conn.execute(
        "UPDATE jobs SET status='running', lease_id=?, "
        "lease_expires_at=datetime('now', ?), started_at=datetime('now') "
        "WHERE id=? AND status='pending' AND (not_before IS NULL OR not_before <= datetime('now'))",
        (lease_id, f"+{lease_seconds} seconds", job_id),
    )
    conn.commit()
    return cur.rowcount == 1


def complete_job(
    conn: sqlite3.Connection, job_id: int, lease_id: str, model_version: str | None = None
) -> bool:
    """Mark a job done, but only if `lease_id` still matches -- a stale
    process whose lease was reclaimed gets rejected here (§5.2)."""
    cur = conn.execute(
        "UPDATE jobs SET status='done', completed_at=datetime('now'), model_version=? "
        "WHERE id=? AND lease_id=? AND status='running'",
        (model_version, job_id, lease_id),
    )
    conn.commit()
    return cur.rowcount == 1


def fail_job(conn: sqlite3.Connection, job_id: int, lease_id: str, error_message: str) -> str:
    """Genuine execution failure. Returns 'retrying', 'failed' (terminal,
    attempts exhausted), or 'lease_lost' (stale lease, no state changed).
    §5.1's retry policy."""
    row = conn.execute(
        "SELECT * FROM jobs WHERE id=? AND lease_id=? AND status='running'", (job_id, lease_id)
    ).fetchone()
    if row is None:
        return "lease_lost"

    attempts = row["attempts"] + 1

    # The recovery policy decides whether retrying is even coherent, not
    # just whether budget remains (§2/§5). A deterministic failure -- bad
    # credentials, a task that cannot be completed, state that moved on --
    # is terminal on the first attempt, because five identical retries of
    # something that cannot succeed cost an hour and change nothing.
    classification = recovery.classify_failure(error_message)
    if attempts < row["max_attempts"] and recovery.should_retry(classification, attempts):
        backoff = compute_error_backoff_seconds(attempts)
        conn.execute(
            "UPDATE jobs SET status='pending', attempts=?, last_error=?, "
            "not_before=datetime('now', ?), wait_reason='error_backoff', "
            "lease_id=NULL, lease_expires_at=NULL WHERE id=? AND lease_id=?",
            (attempts, error_message, f"+{backoff} seconds", job_id, lease_id),
        )
        conn.commit()
        return "retrying"

    conn.execute(
        "UPDATE jobs SET status='failed', attempts=?, last_error=?, completed_at=datetime('now'), "
        "lease_id=NULL, lease_expires_at=NULL WHERE id=? AND lease_id=?",
        (attempts, error_message, job_id, lease_id),
    )
    record_job_event(
        conn,
        job_id=job_id,
        actor_type="daemon",
        actor_id=None,
        event_type="job_failed_terminal",
        target_type=row["target_type"],
        target_id=row["target_id"],
    )
    conn.commit()

    # §18: record what went wrong and what to do differently, so the same
    # dead remediation is not tried again on the next occurrence.
    recovery.record_failure_memory(
        conn,
        job_id=job_id,
        classification=classification,
        symptom=error_message,
        target_type=row["target_type"],
        target_id=row["target_id"],
        failed_remediations=f"retry x{attempts}" if attempts > 1 else "no retry (deterministic)",
    )

    # §17: a terminal failure must actually leave the job not-running with
    # its lease released. If it doesn't, say so rather than reporting a
    # clean failure over a stuck row.
    ok, failed_checks = recovery.verify_recovery(
        conn, job_id, ("job_not_running", "lease_released")
    )
    if not ok:
        return "failed_unverified"
    return "failed"


def block_provider(
    conn: sqlite3.Connection, provider: str, seconds: float, signal: str | None = None
) -> None:
    """Back the whole provider off, not just the job that hit the limit.

    The circuit breaker (§6): a rate limit is a property of the PROVIDER,
    so once one job sees it every other job routed to that provider should
    stop trying. Without this each concurrent worker independently
    rediscovers the same limit -- with four workers that is four wasted
    calls where one would do, and the waste scales with concurrency.
    """
    conn.execute(
        "INSERT INTO provider_state (provider, blocked_until, last_signal) "
        "VALUES (?, datetime('now', ?), ?) "
        "ON CONFLICT(provider) DO UPDATE SET "
        # MAX so a longer backoff already in force is never shortened by a
        # later, smaller one.
        "blocked_until = MAX(COALESCE(blocked_until, ''), excluded.blocked_until), "
        "last_signal = excluded.last_signal",
        (provider, f"+{max(1, int(seconds))} seconds", (signal or "")[:500]),
    )
    conn.commit()


def record_rate_limit(
    conn: sqlite3.Connection,
    job_id: int,
    lease_id: str,
    retry_after_seconds: float | None,
    provider: str | None = None,
) -> bool:
    """A rate limit is not a failure -- stays `pending`, never touches
    `attempts`. Returns False on a stale lease, same as fail_job. §5.3."""
    row = conn.execute(
        "SELECT * FROM jobs WHERE id=? AND lease_id=? AND status='running'", (job_id, lease_id)
    ).fetchone()
    if row is None:
        return False

    rate_limit_count = row["rate_limit_count"] + 1
    backoff = compute_rate_limit_backoff_seconds(rate_limit_count, retry_after_seconds)
    if provider:
        block_provider(conn, provider, backoff, f"rate limited on job {job_id}")
    conn.execute(
        "UPDATE jobs SET status='pending', rate_limit_count=?, "
        "not_before=datetime('now', ?), wait_reason='rate_limited', "
        "lease_id=NULL, lease_expires_at=NULL WHERE id=? AND lease_id=?",
        (rate_limit_count, f"+{backoff} seconds", job_id, lease_id),
    )
    conn.commit()
    return True


def run_with_session(conn: sqlite3.Connection, job_id: int, backend, prompt: str, **opts):
    """Call `backend` for `job_id`, carrying its backend session across attempts.

    Every handler goes through this instead of calling `backend.run`
    directly, so resumption is uniform: attempt 1 starts a fresh session
    and records its id; attempts 2..N resume that session. A job killed by
    token exhaustion mid-derivation therefore continues from where it
    stopped rather than re-deriving (and re-paying for) everything.

    The id is persisted on every outcome, including failures -- that is
    precisely the case it exists for -- and committed immediately, so a
    daemon that dies between the backend call and the job's own state
    write still leaves the session recoverable.
    """
    row = conn.execute("SELECT backend_session_id FROM jobs WHERE id = ?", (job_id,)).fetchone()
    previous = row["backend_session_id"] if row is not None else None
    if previous:
        opts.setdefault("resume_session_id", previous)

    result = backend.run(prompt, **opts)

    session_id = getattr(result, "session_id", None)
    if session_id and session_id != previous:
        conn.execute(
            "UPDATE jobs SET backend_session_id = ? WHERE id = ?", (session_id, job_id)
        )
        conn.commit()
    return result


def reclaim_expired_leases(conn: sqlite3.Connection) -> int:
    """Reset `running` jobs whose lease has expired back to `pending`.
    §5.2 -- this only handles the "lease expired" half; the write-time
    lease-id check in complete_job/fail_job/record_rate_limit is what
    prevents the reclaimed-but-still-alive process from double-applying."""
    cur = conn.execute(
        "UPDATE jobs SET status='pending', lease_id=NULL, lease_expires_at=NULL "
        "WHERE status='running' AND lease_expires_at < datetime('now')"
    )
    conn.commit()
    return cur.rowcount


def cancel_job(conn: sqlite3.Connection, job_id: int, reason: str) -> bool:
    """Cancel a pending job by MARKING it, never by deleting the row.

    Deleting a job row frees its rowid, and SQLite reuses freed rowids --
    so a later INSERT can take the id of a job a daemon still holds in
    flight, and that daemon's writes then land on an unrelated job. This
    was observed once in a live run: a job whose recorded kind and recorded
    event disagreed, because a cancelled-and-recreated row shared an id
    with work still executing.

    Only a pending job can be cancelled. A running one holds a lease; let
    it finish or let the lease expire, so its writes always find the row
    they expect.
    """
    cur = conn.execute(
        "UPDATE jobs SET status='cancelled', completed_at=datetime('now'), last_error=?, "
        "lease_id=NULL, lease_expires_at=NULL WHERE id=? AND status='pending'",
        (f"cancelled: {reason}", job_id),
    )
    conn.commit()
    return cur.rowcount == 1
