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

# How many failed review rounds before the lab stops revising and waits
# for a human. Not a quality judgement -- a stop condition on a loop that
# had none.
MAX_REVIEW_ROUNDS = 4

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


def revise_root_problem(conn: sqlite3.Connection, lab_id: int, root_problem: str) -> list[int]:
    """Replace a failed lab's root problem and start a fresh review round.

    docs/DESIGN.md §3.2's revise-and-resubmit loop, at the lab level: the
    round is bumped BEFORE the new reviewer jobs are enqueued so their
    `reviews` rows validate against `labs.current_review_round`, and the
    previous round's reviews stay on the record -- review history is
    append-only, and a later round's reviewers must never see (or be
    tallied alongside) an earlier round's verdicts.

    Only a lab still in `pending_review` can be revised: rewriting the
    root problem of an `active` lab would silently invalidate every task
    already decomposed from the old one.
    """
    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (lab_id,)).fetchone()
    if lab is None:
        raise LabReviewError(f"no lab with id={lab_id}")
    if lab["status"] != "pending_review":
        raise LabReviewError(
            f"lab {lab_id} is {lab['status']!r}; only a lab still in 'pending_review' "
            "can have its root problem revised"
        )
    if not root_problem.strip():
        raise LabReviewError("revised root problem is empty")

    conn.execute(
        "UPDATE labs SET root_problem = ?, current_review_round = current_review_round + 1 "
        "WHERE id = ?",
        (root_problem.strip(), lab_id),
    )
    conn.commit()
    return request_lab_review(conn, lab_id)


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

    result = jobs.run_with_session(conn, job_id, backend, _build_prompt(lab["root_problem"]))

    if result.rate_limited:
        jobs.record_rate_limit(
            conn, job_id, lease_id, result.retry_after_seconds, provider=backend.name
        )
        return "rate_limited"

    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)

    match = _VERDICT_RE.search(result.text)
    if match is None:
        return jobs.fail_job(
            conn, job_id, lease_id, f"no VERDICT line found in review output: {result.text[:300]}"
        )
    verdict = match.group(1)

    # lab_dir-relative, matching professors.memory_path / tasks.brief_path
    # (see create_prof.persist_professor's note on the double-nesting bug).
    relpath = f"{lab['id']}/reviews/{row['review_round']}/{row['reviewer_index']}.md"
    write_artifact(lab_dir / relpath, result.text)

    conn.execute(
        "INSERT INTO reviews (target_type, target_id, review_round, reviewer_index, verdict, rationale_path, reviewer_backend) "
        "VALUES ('lab', ?, ?, ?, ?, ?, ?)",
        (
            lab["id"], row["review_round"], row["reviewer_index"], verdict,
            relpath, backend.name,
        ),
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
    elif review_round >= MAX_REVIEW_ROUNDS:
        # The revise->review cycle is otherwise UNBOUNDED, and each turn
        # costs 3 reviews plus a revision. Lab #3 burned 12 reviews and 2
        # revisions oscillating around the bar -- 2-of-3 accept but only
        # one strong_accept -- with no mechanism that could ever stop it.
        #
        # Stopping here rather than revising again is the point: a problem
        # statement that four independent panels declined to endorse is
        # not one more rewrite away from passing, and quota spent looping
        # is quota not spent on labs doing research. The lab stays in
        # pending_review so nothing is lost; a human decides whether to
        # push it through, rewrite the root problem, or drop it.
        event_type = "lab_review_exhausted"
    else:
        event_type = "lab_review_failed"
        # Without this the lab sits in pending_review with nothing queued
        # and is never worked on again -- the dead end that stranded three
        # labs simultaneously.
        request_lab_revision(conn, lab_id)

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


REVISE_PROMPT_TEMPLATE = """You are {name}, a professor in {field}. Your lab's root problem was \
submitted to three independent reviewers and FAILED: it needs 2 of 3 strong_accept and did not \
get them.

Your current root problem:
<root_problem>
{root_problem}
</root_problem>

The reviewers said this. They did not see each other's reviews, so where two raise the same point \
independently, treat it as certainly real:
<reviews>
{reviews}
</reviews>

Revise the root problem so it survives review, WITHOUT abandoning the research question that \
motivated it. Specifically:

- Fix every concrete defect they name -- undefined terms, unstated conventions, a prior result the \
problem collapses into, a scope that no sequence of tasks could make progress on.
- Do not simply narrow the ambition to make it safe. A problem watered down until it is trivially \
tractable fails a different criterion.
- If a reviewer shows your problem is already solved or follows from known work, that is real \
information: re-centre on what genuinely remains open, and say what the known result settles.
- Keep what was right. Do not rewrite passages nobody objected to.

Respond with ONLY the revised root problem statement as plain prose. No preamble, no commentary \
about what you changed.
"""


def request_lab_revision(conn: sqlite3.Connection, lab_id: int) -> int | None:
    """Queue a professor revision after a failed review round.

    Without this a failed lab review is a dead end: the lab sits in
    `pending_review` with nothing queued and no work is ever dispatched
    against it again. That is the same gap `student_revise_paper` closed
    for rejected papers, one level up -- and it stranded three labs at
    once before it existed.
    """
    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (lab_id,)).fetchone()
    if lab is None or lab["status"] != "pending_review":
        return None
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE kind='lab_revise' AND target_id=? "
        "AND status IN ('pending','running')",
        (lab_id,),
    ).fetchone()["n"]
    if pending:
        return None
    cur = conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status) "
        "VALUES ('lab_revise', 'lab', ?, 'pending')",
        (lab_id,),
    )
    conn.commit()
    return cur.lastrowid


def execute_lab_revise_job(
    conn: sqlite3.Connection, job_id: int, backend: Backend, lab_dir: Path
) -> str:
    """Rewrite the root problem against the reviewers' objections, then
    resubmit for a fresh round."""
    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=1800):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (row["target_id"],)).fetchone()
    if lab is None:
        return jobs.fail_job(conn, job_id, lease_id, f"no lab with id={row['target_id']}")
    if lab["status"] != "pending_review":
        jobs.complete_job(conn, job_id, lease_id)
        return "done"

    professor = conn.execute(
        "SELECT * FROM professors WHERE id = ?", (lab["professor_id"],)
    ).fetchone()
    reviews = conn.execute(
        "SELECT * FROM reviews WHERE target_type='lab' AND target_id=? AND review_round=? "
        "ORDER BY reviewer_index",
        (lab["id"], lab["current_review_round"]),
    ).fetchall()
    if not reviews:
        return jobs.fail_job(conn, job_id, lease_id, "no reviews to revise against")

    parts = []
    for review in reviews:
        path = lab_dir / review["rationale_path"]
        body = path.read_text(errors="replace") if path.exists() else "(rationale missing)"
        parts.append(f"--- Reviewer {review['reviewer_index']} ({review['verdict']}) ---\n{body}")

    result = jobs.run_with_session(
        conn,
        job_id,
        backend,
        REVISE_PROMPT_TEMPLATE.format(
            name=professor["name"],
            field=professor["field"],
            root_problem=lab["root_problem"],
            reviews="\n\n".join(parts),
        ),
    )

    if result.rate_limited:
        jobs.record_rate_limit(
            conn, job_id, lease_id, result.retry_after_seconds, provider=backend.name
        )
        return "rate_limited"
    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)
    if len(result.text.split()) < 60:
        return jobs.fail_job(
            conn, job_id, lease_id,
            f"revised root problem too short to be serious ({len(result.text.split())} words)",
        )

    try:
        revise_root_problem(conn, lab["id"], result.text)
    except LabReviewError as e:
        return jobs.fail_job(conn, job_id, lease_id, str(e))

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"
    record_job_event(
        conn, job_id=job_id, actor_type="professor", actor_id=professor["id"],
        event_type="lab_revised", target_type="lab", target_id=lab["id"],
    )
    conn.commit()
    return "done"
