"""Paper review: 3 independent reviewers, 2-of-3 strong_accept to pass.

docs/DESIGN.md §3.2/§4. Deliberately the same shape as lab_review.py --
enqueue N independent jobs, each parses one VERDICT line, the Nth review
to land triggers the tally -- because the isolation property is the whole
point: no reviewer ever sees another's output, and nothing is tallied
until every reviewer has reported.

Unlike lab review, this one uses templates/review_rubric.md (the rubric
for a *completed* document: novelty, correctness, completeness,
significance) rather than the well-posedness rubric a bare problem
statement gets.
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

_RUBRIC_PATH = db.REPO_ROOT / "templates" / "review_rubric.md"
_VERDICT_RE = re.compile(r"^VERDICT:\s*(\w+)\s*$", re.MULTILINE)
_LEADING_HTML_COMMENT_RE = re.compile(r"^\s*<!--.*?-->\s*\n", re.DOTALL)

DOCUMENT_TYPE = "a research paper submitted to an automated lab"


class PaperReviewError(RuntimeError):
    pass


def build_review_prompt(document: str, document_type: str = DOCUMENT_TYPE) -> str:
    """Fill the shared rubric. The rubric is fed verbatim apart from its
    two placeholders -- it's the same standard paper and defense review
    are held to, so it must not be paraphrased per caller.

    Uses replace() rather than str.format(): the document under review is
    HTML full of CSS braces, and format() would try to interpret every one
    of them as a field.
    """
    template = _RUBRIC_PATH.read_text()
    # Strip the leading authoring comment -- documentation for editors of
    # the rubric file, not an instruction to the reviewer.
    template = _LEADING_HTML_COMMENT_RE.sub("", template, count=1)
    return template.replace("{DOCUMENT_TYPE}", document_type).replace(
        "{DOCUMENT_CONTENT}", document
    )


def request_paper_review(conn: sqlite3.Connection, paper_id: int) -> list[int]:
    """Enqueue REVIEWER_COUNT independent paper_review jobs for the
    paper's current review_round. Raises if that round was already
    requested -- a double dispatch would corrupt the tally."""
    paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if paper is None:
        raise PaperReviewError(f"no paper with id={paper_id}")

    round_ = paper["review_round"]
    existing = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE kind='paper_review' AND target_type='paper' "
        "AND target_id=? AND review_round=?",
        (paper_id, round_),
    ).fetchone()["n"]
    if existing > 0:
        raise PaperReviewError(f"paper {paper_id} round {round_} review already requested")

    job_ids = []
    for reviewer_index in range(1, REVIEWER_COUNT + 1):
        cur = conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status, review_round, reviewer_index) "
            "VALUES ('paper_review', 'paper', ?, 'pending', ?, ?)",
            (paper_id, round_, reviewer_index),
        )
        job_ids.append(cur.lastrowid)
    conn.commit()
    return job_ids


def execute_paper_review_job(
    conn: sqlite3.Connection, job_id: int, backend: Backend, lab_dir: Path
) -> str:
    """Daemon special_handlers signature: (conn, job_id, backend, lab_dir)."""
    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=3600):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    paper = conn.execute("SELECT * FROM papers WHERE id = ?", (row["target_id"],)).fetchone()
    if paper is None:
        return jobs.fail_job(conn, job_id, lease_id, f"paper {row['target_id']} no longer exists")

    # A review job that outlived its round (the paper was revised and
    # resubmitted while this job sat in backoff) must not write a review
    # row -- the trg_reviews_valid_target trigger would reject it anyway,
    # but failing here gives a legible error instead of a trigger abort.
    if row["review_round"] != paper["review_round"]:
        return jobs.fail_job(
            conn,
            job_id,
            lease_id,
            f"job is for round {row['review_round']} but paper {paper['id']} "
            f"is now on round {paper['review_round']}",
        )

    paper_file = lab_dir / paper["path"]
    if not paper_file.exists():
        return jobs.fail_job(conn, job_id, lease_id, f"paper file missing: {paper['path']}")

    result = backend.run(build_review_prompt(paper_file.read_text()))

    if result.rate_limited:
        jobs.record_rate_limit(conn, job_id, lease_id, result.retry_after_seconds)
        return "rate_limited"
    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)

    # Last match, not first: a reviewer will sometimes quote the required
    # format while explaining itself before emitting the real verdict.
    matches = _VERDICT_RE.findall(result.text)
    if not matches:
        return jobs.fail_job(
            conn, job_id, lease_id, f"no VERDICT line found in review output: {result.text[:300]}"
        )
    verdict = matches[-1]

    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (paper["task_id"],)).fetchone()
    relpath = (
        f"{task['lab_id']}/tasks/{paper['task_id']}/papers/{paper['id']}"
        f"/reviews/{row['review_round']}/{row['reviewer_index']}.md"
    )
    write_artifact(lab_dir / relpath, result.text)

    conn.execute(
        "INSERT INTO reviews (target_type, target_id, review_round, reviewer_index, verdict, rationale_path) "
        "VALUES ('paper', ?, ?, ?, ?, ?)",
        (paper["id"], row["review_round"], row["reviewer_index"], verdict, relpath),
    )

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"

    record_job_event(
        conn,
        job_id=job_id,
        actor_type="reviewer",
        actor_id=None,
        event_type="paper_review_verdict_recorded",
        target_type="paper",
        target_id=paper["id"],
        payload_path=relpath,
    )
    conn.commit()

    _maybe_finalize(conn, paper["id"], row["review_round"], job_id)
    return "done"


def _maybe_finalize(conn: sqlite3.Connection, paper_id: int, review_round: int, job_id: int) -> None:
    """Once all REVIEWER_COUNT reviews for this round are in, tally.

    Pass  -> paper accepted, student back to 'working', task handed to the
             professor for the §3.3 callback decision.
    Fail  -> paper rejected, student back to 'working' to revise; a fresh
             round is NOT auto-requested, because §3.3 gives the professor
             the choice between revising, re-scoping, and abandoning.
    """
    reported = conn.execute(
        "SELECT COUNT(*) AS n FROM reviews WHERE target_type='paper' AND target_id=? AND review_round=?",
        (paper_id, review_round),
    ).fetchone()["n"]
    if reported < REVIEWER_COUNT:
        return

    strong = conn.execute(
        "SELECT COUNT(*) AS n FROM reviews WHERE target_type='paper' AND target_id=? "
        "AND review_round=? AND verdict='strong_accept'",
        (paper_id, review_round),
    ).fetchone()["n"]

    paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    passed = strong >= STRONG_ACCEPT_THRESHOLD

    conn.execute(
        "UPDATE papers SET status = ? WHERE id = ?",
        ("accepted" if passed else "rejected", paper_id),
    )
    conn.execute("UPDATE students SET status = 'working' WHERE id = ?", (paper["student_id"],))

    if passed:
        conn.execute(
            "UPDATE tasks SET status = 'pending_prof_review' WHERE id = ?", (paper["task_id"],)
        )

    record_job_event(
        conn,
        job_id=job_id,
        actor_type="daemon",
        actor_id=None,
        event_type="paper_accepted" if passed else "paper_rejected",
        target_type="paper",
        target_id=paper_id,
    )
    conn.commit()


def resubmit_paper(conn: sqlite3.Connection, paper_id: int) -> list[int]:
    """Start a fresh review round on a rejected paper (§3.2 step 4).

    Bumps papers.review_round first so the new `reviews` rows validate
    against it, then enqueues a fresh independent reviewer set. Prior
    rounds' reviews stay on the record -- review history is append-only.
    """
    paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if paper is None:
        raise PaperReviewError(f"no paper with id={paper_id}")
    if paper["status"] != "rejected":
        raise PaperReviewError(
            f"paper {paper_id} is {paper['status']!r}, only a rejected paper can be resubmitted"
        )

    conn.execute(
        "UPDATE papers SET review_round = review_round + 1, status = 'in_review' WHERE id = ?",
        (paper_id,),
    )
    conn.commit()
    return request_paper_review(conn, paper_id)
