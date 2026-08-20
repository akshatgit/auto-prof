"""Append-only audit log helpers, backing the `events` table.

Two entry points: `record_job_event` for AI-agent actions that trace back
to a completed job (docs/DESIGN.md §6.1), and `record_human_event` for
manual overrides (stop/resume/edit/replay -- docs/TASKS.md Phase 3) which
have no job_id. Both funnel through the same table so the audit trail
never has a gap between "what the daemon did" and "what a human did."
"""

import json
import sqlite3


def record_job_event(
    conn: sqlite3.Connection,
    job_id: int,
    actor_type: str,
    actor_id: int | None,
    event_type: str,
    target_type: str,
    target_id: int,
    payload_path: str | None = None,
    metadata: dict | None = None,
) -> int:
    """`metadata` is small structured detail about the event itself (the
    failure class behind an escalation, say). The column already existed
    and nothing could write it, so events that needed a reason carried
    none."""
    cur = conn.execute(
        "INSERT INTO events (job_id, actor_type, actor_id, event_type, target_type, target_id, "
        "payload_path, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, actor_type, actor_id, event_type, target_type, target_id, payload_path,
         json.dumps(metadata, sort_keys=True) if metadata is not None else None),
    )
    return cur.lastrowid


def record_human_event(
    conn: sqlite3.Connection,
    event_type: str,
    target_type: str,
    target_id: int,
    actor_id: int | None = None,
    payload_path: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO events (job_id, actor_type, actor_id, event_type, target_type, target_id, payload_path) "
        "VALUES (NULL, 'human', ?, ?, ?, ?, ?)",
        (actor_id, event_type, target_type, target_id, payload_path),
    )
    return cur.lastrowid
