"""Lab review: vets a professor's root-problem "soul" before the lab is
allowed to dispatch any work, and propagates a passing review downstream
by activating the lab and auto-enqueuing its first professor_decompose
job. Same isolation/tally shape as the paper/defense review pipeline in
docs/DESIGN.md §4 (independent reviewers, no partial tallying), scoped to
3 reviewers / 2-of-3 strong_accept -- the same bar as paper review, since
a lab's root problem is exactly as consequential as a paper's claim: it's
what years of downstream task work will be judged against.
"""

import re
import sqlite3
import uuid
from pathlib import Path

from . import db, jobs
from .artifacts import write_artifact
from .backends.base import Backend
from .events import record_job_event

REVIEWER_COUNT = 3
STRONG_ACCEPT_THRESHOLD = 2

_LAB_REVIEW_RUBRIC_PATH = db.REPO_ROOT / "templates" / "lab_review_rubric.md"
_VERDICT_RE = re.compile(r"^VERDICT:\s*(\w+)\s*$", re.MULTILINE)


class LabReviewError(RuntimeError):
    pass


_LEADING_HTML_COMMENT_RE = re.compile(r"^\s*<!--.*?-->\s*\n", re.DOTALL)


def _build_prompt(root_problem: str) -> str:
    # Deliberately NOT templates/review_rubric.md -- that rubric evaluates
    # a completed paper/defense (proof present, Related Work present) and
    # was tried here first; it systematically rejects a bare problem
    # statement for lacking things it isn't supposed to have yet. See
    # templates/lab_review_rubric.md's header comment for the full story.
    template = _LAB_REVIEW_RUBRIC_PATH.read_text()
    # Strip the leading HTML comment: it's authoring documentation for
    # future editors of this file, not an instruction for the reviewer,
    # and shouldn't be sent as part of the actual prompt.
    template = _LEADING_HTML_COMMENT_RE.sub("", template, count=1)
    return template.format(ROOT_PROBLEM=root_problem)


def request_lab_review(conn: sqlite3.Connection, lab_id: int) -> list[int]:
    """Enqueue REVIEWER_COUNT independent lab_review jobs for the lab's
    current_review_round. Raises if the lab doesn't exist or a review for
    this round was already requested (idempotency guard -- prevents
    double-dispatch, which would corrupt the tally)."""
    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (lab_id,)).fetchone()
    if lab is None:
        raise LabReviewError(f"no lab with id={lab_id}")

    round_ = lab["current_review_round"]
    existing = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE kind='lab_review' AND target_type='lab' "
        "AND target_id=? AND review_round=?",
        (lab_id, round_),
    ).fetchone()
    if existing["n"] > 0:
        raise LabReviewError(f"lab {lab_id} round {round_} review already requested")

    job_ids = []
    for reviewer_index in range(1, REVIEWER_COUNT + 1):
        cur = conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status, review_round, reviewer_index) "
            "VALUES ('lab_review', 'lab', ?, 'pending', ?, ?)",
            (lab_id, round_, reviewer_index),
        )
        job_ids.append(cur.lastrowid)
    conn.commit()
    return job_ids


def execute_lab_review_job(
    conn: sqlite3.Connection, job_id: int, backend: Backend, lab_dir: Path
) -> str:
    """Matches the daemon's special_handlers signature: (conn, job_id,
    backend, lab_dir) -> outcome string."""
    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=1800):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (row["target_id"],)).fetchone()
    if lab is None:
        return jobs.fail_job(conn, job_id, lease_id, f"lab {row['target_id']} no longer exists")

    result = backend.run(_build_prompt(lab["root_problem"]))

    if result.rate_limited:
        jobs.record_rate_limit(conn, job_id, lease_id, result.retry_after_seconds)
        return "rate_limited"

    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)

    match = _VERDICT_RE.search(result.text)
    if match is None:
        return jobs.fail_job(
            conn, job_id, lease_id, f"no VERDICT line found in review output: {result.text[:300]}"
        )
    verdict = match.group(1)

    relpath = f"labs/{lab['id']}/reviews/{row['review_round']}/{row['reviewer_index']}.md"
    write_artifact(lab_dir / relpath, result.text)

    conn.execute(
        "INSERT INTO reviews (target_type, target_id, review_round, reviewer_index, verdict, rationale_path) "
        "VALUES ('lab', ?, ?, ?, ?, ?)",
        (lab["id"], row["review_round"], row["reviewer_index"], verdict, relpath),
    )

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"

    record_job_event(
        conn,
        job_id=job_id,
        actor_type="reviewer",
        actor_id=None,
        event_type="lab_review_verdict_recorded",
        target_type="lab",
        target_id=lab["id"],
        payload_path=relpath,
    )
    conn.commit()

    _maybe_finalize(conn, lab["id"], row["review_round"], job_id)
    return "done"


def _maybe_finalize(conn: sqlite3.Connection, lab_id: int, review_round: int, job_id: int) -> None:
    """Once all REVIEWER_COUNT reviews for this round are in, tally and
    -- if passed -- propagate downstream: activate the lab and enqueue
    its first professor_decompose job, all in one commit."""
    reported = conn.execute(
        "SELECT COUNT(*) AS n FROM reviews WHERE target_type='lab' AND target_id=? AND review_round=?",
        (lab_id, review_round),
    ).fetchone()["n"]
    if reported < REVIEWER_COUNT:
        return

    strong = conn.execute(
        "SELECT COUNT(*) AS n FROM reviews WHERE target_type='lab' AND target_id=? "
        "AND review_round=? AND verdict='strong_accept'",
        (lab_id, review_round),
    ).fetchone()["n"]

    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (lab_id,)).fetchone()

    if strong >= STRONG_ACCEPT_THRESHOLD:
        conn.execute("UPDATE labs SET status = 'active' WHERE id = ?", (lab_id,))
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('professor_decompose', 'professor', ?, 'pending')",
            (lab["professor_id"],),
        )
        event_type = "lab_review_passed"
    else:
        event_type = "lab_review_failed"

    record_job_event(
        conn,
        job_id=job_id,
        actor_type="daemon",
        actor_id=None,
        event_type=event_type,
        target_type="lab",
        target_id=lab_id,
    )
    conn.commit()
