"""Ties lease claim + backend dispatch + artifact write + completion
together into one job execution -- the unit of work underneath
docs/DESIGN.md §5's tick loop (see autoprof/daemon.py for the loop
itself).
"""

import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import jobs
from .artifacts import write_artifact
from .backends.base import Backend
from .events import record_job_event

DEFAULT_LEASE_SECONDS = 1800


@dataclass
class PromptSpec:
    """What a prompt-builder function returns for one job."""

    prompt: str
    artifact_relpath: str | None  # relative to lab_dir; None if this job kind produces no file
    event_type: str
    actor_type: str
    actor_id: int | None


def execute_job(
    conn: sqlite3.Connection,
    job_id: int,
    backend: Backend,
    prompt_builders: dict,
    lab_dir: Path,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> str:
    """Run one job to completion (or backoff). Returns one of:
    'not_claimed', 'done', 'retrying', 'failed', 'rate_limited'.

    Never raises for expected failure modes (backend errors, a missing or
    broken prompt builder) -- those all resolve to a job state transition
    via jobs.fail_job, matching Backend.run()'s own "report, don't raise"
    contract (autoprof/backends/base.py).
    """
    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()

    builder = prompt_builders.get(row["kind"])
    if builder is None:
        return jobs.fail_job(
            conn, job_id, lease_id, f"no prompt builder registered for job kind {row['kind']!r}"
        )

    try:
        spec = builder(conn, row)
    except Exception as e:  # noqa: BLE001 -- any builder failure must become a job failure, not a crash
        return jobs.fail_job(conn, job_id, lease_id, f"prompt builder raised: {e}")

    result = backend.run(spec.prompt)

    if result.rate_limited:
        jobs.record_rate_limit(conn, job_id, lease_id, result.retry_after_seconds)
        return "rate_limited"

    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)

    if spec.artifact_relpath is not None:
        write_artifact(lab_dir / spec.artifact_relpath, result.text)

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        # Lease was reclaimed out from under us between claim and here --
        # the artifact write above is idempotent (§5.2) so this is safe to
        # just report; a future retry (by whoever now holds the job) will
        # overwrite the same artifact path.
        return "not_claimed"

    record_job_event(
        conn,
        job_id=job_id,
        actor_type=spec.actor_type,
        actor_id=spec.actor_id,
        event_type=spec.event_type,
        target_type=row["target_type"],
        target_id=row["target_id"],
        payload_path=spec.artifact_relpath,
    )
    conn.commit()
    return "done"
