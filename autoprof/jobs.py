"""Job lease/retry state machine -- docs/DESIGN.md §5.1/§5.2/§5.3.

Pure DB state transitions: no backend calls happen here (see
autoprof/runner.py for the piece that actually dispatches to a Backend).
Kept separate so this state machine is testable without ever touching a
subprocess or the network.
"""

import re
import sqlite3
from pathlib import Path

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
    # RecoveryPolicy.escalate meant "surface to a human rather than fail
    # silently", but nothing ever read it, so an escalating class died as
    # quietly as a routine one. A provider refusal is the case that made
    # this matter: lab #9's supervision was refused on content grounds,
    # went terminal, and the lab simply stopped with no pending job and no
    # signal anywhere. The distinct event is what `autoprof status
    # --blocked` reads, so a stuck lab is discoverable without grepping
    # last_error across the jobs table.
    if recovery.lookup(classification).escalate:
        record_job_event(
            conn,
            job_id=job_id,
            actor_type="daemon",
            actor_id=None,
            event_type="job_escalated",
            target_type=row["target_type"],
            target_id=row["target_id"],
            metadata={"classification": classification, "kind": row["kind"]},
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


# Rust `tracing` lines: an ISO timestamp then a level. With
# AUTOPROF_CODEX_RUST_LOG set to trace these are 94% of a job's output --
# 809 of 864 lines in one measured job -- and they bury the 20 lines that
# say what the model actually did.
_TRACE_LINE = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z?\s+(TRACE|DEBUG)\s")


def is_noise(line: str) -> bool:
    """True for backend framework logging that hides the actual work."""
    return bool(_TRACE_LINE.match(line.lstrip()))


# A live window on a running job, not an archive: enough to see what it is
# doing now, capped so a chatty backend cannot fill the disk.
JOB_LOG_MAX_BYTES = 2_000_000


def job_log_path(db_path, job_id: int) -> Path:
    """Where a running job's streamed output is tailed from.

    Beside the database rather than in lab_dir: the writer is
    run_with_session, which knows the connection but not which lab it is
    serving.
    """
    return Path(db_path).parent / "joblogs" / f"{job_id}.log"


def _progress_recorder(conn: sqlite3.Connection, job_id: int, every_seconds: float = 15.0):
    """Return an on_progress callback that persists work evidence.

    Writes are throttled: a chatty backend emits thousands of lines and the
    point is a heartbeat, not a transaction per token. Uses its own
    connection because the callback runs on the backend's reader thread
    while the caller may be using `conn`.
    """
    import sqlite3 as _sqlite3
    import time as _time

    from .backends.progress import Progress

    db_path = None
    try:
        for _, name, filename in conn.execute("PRAGMA database_list"):
            if name == "main" and filename:
                db_path = filename
                break
    except _sqlite3.Error:
        db_path = None
    if not db_path:
        return None

    progress = Progress()
    state = {"last_write": 0.0}
    lease_seconds = 1800

    # Stream the job's output to a file so a read-only view can tail it
    # while the job runs. Capped: this is a live window, not an archive --
    # the durable record is the artifact the handler writes on completion.
    log_path = job_log_path(db_path, job_id)
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("")
    except OSError:
        log_path = None

    def write_now(sample: bool = False):
        try:
            side = _sqlite3.connect(db_path, timeout=5)
            try:
                side.execute(
                    "UPDATE jobs SET progress_at = datetime('now'), progress_tokens = ?, "
                    "progress_items = ?, progress_input_tokens = ?, "
                    "progress_cached_tokens = ? WHERE id = ?",
                    (progress.produced_tokens, progress.items, progress.input_tokens,
                     progress.cached_tokens, job_id),
                )
                if sample:
                    # A cumulative point in a time series. Rate is a difference
                    # between two of these; the job row alone only ever holds
                    # the latest total.
                    side.execute(
                        "INSERT INTO token_samples (job_id, backend, backend_model, "
                        "produced_tokens, input_tokens, cached_tokens) "
                        "SELECT ?, backend, backend_model, ?, ?, ? FROM jobs WHERE id = ?",
                        (job_id, progress.produced_tokens, progress.input_tokens,
                         progress.cached_tokens, job_id),
                    )
                side.commit()
            finally:
                side.close()
        except _sqlite3.Error:
            pass

    def record(_stream, chunk, _total):
        text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", "replace")
        progress.feed(text)
        if log_path is not None and not is_noise(text):
            try:
                if log_path.exists() and log_path.stat().st_size < JOB_LOG_MAX_BYTES:
                    with log_path.open("a", encoding="utf-8") as handle:
                        handle.write(text)
            except OSError:
                pass
        now = _time.monotonic()
        if now - state["last_write"] < every_seconds:
            return
        state["last_write"] = now
        write_now(sample=True)
        try:
            side = _sqlite3.connect(db_path, timeout=5)
            try:
                side.execute(
                    "UPDATE jobs SET progress_at = datetime('now'), progress_tokens = ?, "
                    "progress_items = ?, lease_expires_at = datetime('now', ?) "
                    "WHERE id = ? AND status = 'running'",
                    (progress.produced_tokens, progress.items,
                     f"+{lease_seconds} seconds", job_id),
                )
                side.commit()
            finally:
                side.close()
        except _sqlite3.Error:
            pass   # a heartbeat failing must never fail the research job

    # Codex reports token usage only at `turn.completed`, which lands at the
    # very end of a call -- the throttle above then skips it and the finished
    # job keeps whatever stale count the last heartbeat wrote, usually zero.
    # The flush is what makes the final number true.
    record.flush = lambda: write_now(sample=True)
    record.progress = progress
    return record


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
    row = conn.execute(
        "SELECT backend_session_id, backend FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    previous = row["backend_session_id"] if row is not None else None
    # A session belongs to the backend that opened it. When a lab's review
    # panel or generation backend is reconfigured, a job can be retried on a
    # DIFFERENT backend while still holding the old one's session id -- and
    # handing a Codex thread id to Claude fails with "No conversation found",
    # burning attempts on a job that is otherwise fine. Resume only within
    # the backend that created the session.
    owner = row["backend"] if row is not None else None
    if previous and owner and owner != getattr(backend, "name", owner):
        previous = None
        conn.execute(
            "UPDATE jobs SET backend_session_id = NULL WHERE id = ?", (job_id,)
        )
        conn.commit()
    if previous:
        opts.setdefault("resume_session_id", previous)

    # Report work as it happens, and extend the lease while it does. A job
    # producing tokens is working however long it takes; the lease should
    # not expire underneath it and invite a reclaim of live work.
    opts.setdefault("on_progress", _progress_recorder(conn, job_id))

    result = backend.run(prompt, **opts)

    recorder = opts.get("on_progress")
    flush = getattr(recorder, "flush", None)
    if callable(flush):
        flush()

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
    # Count the expiry as an attempt. Dispatch orders by attempts before age,
    # so without this a job that keeps outrunning its lease keeps its place at
    # the head of the queue and re-takes its lab's workspace lock on every
    # tick -- observed live as lab 8's oldest job blocking two sibling tasks
    # for over three hours while never finishing a round itself.
    cur = conn.execute(
        "UPDATE jobs SET status='pending', lease_id=NULL, lease_expires_at=NULL, "
        "attempts = attempts + 1 "
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
