"""Manual student lifecycle controls: edit / stop / resume / replay.

This is a human override layer that sits alongside the autonomous job
queue (docs/TASKS.md Phase 3), not inside it -- every action here is
audited via autoprof/events.py as a 'human' actor, distinct from the
daemon's own job-driven events (docs/DESIGN.md §6.1).
"""

import sqlite3
from pathlib import Path

from . import db
from .events import record_human_event

VALID_STUDENT_STATUSES = {
    "working", "writing_paper", "in_review", "defending", "graduated", "stuck", "unassigned",
}


class StudentControlError(RuntimeError):
    pass


class StudentNotFoundError(StudentControlError):
    pass


def list_students(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM students ORDER BY id").fetchall()


def get_student(conn: sqlite3.Connection, student_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if row is None:
        raise StudentNotFoundError(f"no student with id={student_id}")
    return row


def stop_student(conn: sqlite3.Connection, student_id: int) -> bool:
    """Pause a student. Idempotent: returns False (no-op, no duplicate
    event) if already paused. `status` is left untouched -- pausing is
    orthogonal to it, see docs/schema.sql's students.paused_at comment."""
    row = get_student(conn, student_id)
    if row["paused_at"] is not None:
        return False
    conn.execute(
        "UPDATE students SET paused_at = datetime('now') WHERE id = ?", (student_id,)
    )
    record_human_event(
        conn, event_type="student_stopped", target_type="student", target_id=student_id
    )
    conn.commit()
    return True


def resume_student(conn: sqlite3.Connection, student_id: int) -> bool:
    """Un-pause a student. Idempotent: returns False if not paused."""
    row = get_student(conn, student_id)
    if row["paused_at"] is None:
        return False
    conn.execute("UPDATE students SET paused_at = NULL WHERE id = ?", (student_id,))
    record_human_event(
        conn, event_type="student_resumed", target_type="student", target_id=student_id
    )
    conn.commit()
    return True


def edit_student(
    conn: sqlite3.Connection,
    student_id: int,
    status: str | None = None,
    memory_text: str | None = None,
    lab_dir: Path | None = None,
) -> None:
    """Directly override a student's status and/or memory.md content.

    At least one of `status`/`memory_text` is required. Every edit is
    recorded as a 'human' event so an automated agent's later behavior
    (which reads memory.md, per docs/DESIGN.md §6.2) can be traced back to
    a manual intervention rather than looking like the agent's own
    reasoning produced the change.
    """
    row = get_student(conn, student_id)

    if status is None and memory_text is None:
        raise StudentControlError("edit_student requires at least one of status= or memory_text=")

    if status is not None:
        if status not in VALID_STUDENT_STATUSES:
            raise StudentControlError(
                f"invalid status {status!r}; must be one of {sorted(VALID_STUDENT_STATUSES)}"
            )
        conn.execute("UPDATE students SET status = ? WHERE id = ?", (status, student_id))

    if memory_text is not None:
        lab_dir = lab_dir if lab_dir is not None else db.LAB_DIR
        memory_path = lab_dir / row["memory_path"] if not Path(row["memory_path"]).is_absolute() else Path(row["memory_path"])
        memory_path.parent.mkdir(parents=True, exist_ok=True)
        memory_path.write_text(memory_text)

    record_human_event(
        conn, event_type="student_edited", target_type="student", target_id=student_id
    )
    conn.commit()


def replay_job(conn: sqlite3.Connection, job_id: int) -> int:
    """Re-run a past job: creates a new `pending` job with the same
    kind/target, linked back via replayed_from_job_id, leaving the
    original job's row untouched in the audit trail (docs/TASKS.md Phase 3).

    Only jobs in a terminal state (`done`/`failed`) can be replayed -- a
    `pending`/`running` job is, by definition, not finished yet, so
    "replaying" it would be ambiguous with just letting it run.
    """
    original = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if original is None:
        raise StudentControlError(f"no job with id={job_id}")
    if original["status"] not in ("done", "failed"):
        raise StudentControlError(
            f"cannot replay job {job_id}: status is {original['status']!r}, "
            "only 'done'/'failed' jobs can be replayed"
        )

    cur = conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status, replayed_from_job_id) "
        "VALUES (?, ?, ?, 'pending', ?)",
        (original["kind"], original["target_type"], original["target_id"], job_id),
    )
    new_job_id = cur.lastrowid

    event_id = record_human_event(
        conn,
        event_type="job_replayed",
        target_type="job",
        target_id=job_id,
    )
    # Link the new job onto that same event row -- record_human_event
    # itself has no job_id parameter (job_id is NULL for pure-human
    # events), so this is a targeted follow-up update rather than a
    # second, redundant event.
    conn.execute("UPDATE events SET job_id = ? WHERE id = ?", (new_job_id, event_id))
    conn.commit()
    return new_job_id
