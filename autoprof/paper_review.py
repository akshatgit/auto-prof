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

from . import callback, collaboration, config, db, jobs, references
from .artifacts import write_artifact
from .backends.base import Backend
from .events import record_job_event

REVIEWER_COUNT = 3
STRONG_ACCEPT_THRESHOLD = 2

# The revise-and-resubmit loop stops when the LAB has enough accepted
# papers, not after a fixed number of rounds -- see autoprof/config.py's
# max_accepted_papers. A round cap measured effort spent and abandoned
# papers whose only defect was fixable; the accepted-paper target measures
# what the lab actually produced.

_RUBRIC_PATH = db.REPO_ROOT / "templates" / "review_rubric.md"
_VERDICT_RE = re.compile(r"^VERDICT:\s*(\w+)\s*$", re.MULTILINE)
# A reviewer may ask the authors for evidence instead of deciding. The
# block runs to the end of the reply, so the whole tail is the request.
_REQUEST_RE = re.compile(r"^REQUEST:\s*$(.*)", re.MULTILINE | re.DOTALL)
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

    prompt = build_review_prompt(paper_file.read_text()) + exchange_transcript(
        conn, paper["id"], row["review_round"], row["reviewer_index"], lab_dir
    )
    result = jobs.run_with_session(conn, job_id, backend, prompt)

    if result.rate_limited:
        jobs.record_rate_limit(
            conn, job_id, lease_id, result.retry_after_seconds, provider=backend.name
        )
        return "rate_limited"
    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)

    # Last match, not first: a reviewer will sometimes quote the required
    # format while explaining itself before emitting the real verdict.
    matches = _VERDICT_RE.findall(result.text)
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (paper["task_id"],)).fetchone()

    if not matches:
        return jobs.fail_job(
            conn, job_id, lease_id, f"no VERDICT line found in review output: {result.text[:300]}"
        )
    verdict = matches[-1]

    relpath = (
        f"{task['lab_id']}/tasks/{paper['task_id']}/papers/{paper['id']}"
        f"/reviews/{row['review_round']}/{row['reviewer_index']}.md"
    )
    write_artifact(lab_dir / relpath, result.text)

    # Upsert, not insert: a reviewer that opened an exchange already has a
    # row for this round, and its second turn revises that verdict rather
    # than filing a second one. UNIQUE(target, round, reviewer_index)
    # guarantees at most one verdict per reviewer either way.
    conn.execute(
        "INSERT INTO reviews (target_type, target_id, review_round, reviewer_index, verdict, "
        "rationale_path, reviewer_backend) VALUES ('paper', ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (target_type, target_id, review_round, reviewer_index) "
        "DO UPDATE SET verdict=excluded.verdict, rationale_path=excluded.rationale_path, "
        "reviewer_backend=excluded.reviewer_backend",
        (
            paper["id"], row["review_round"], row["reviewer_index"], verdict,
            relpath, backend.name,
        ),
    )

    # The verdict above stands on the record whatever happens next. If the
    # reviewer also asked the authors for something, it is saying "this is
    # my judgement as things stand, and here is what could change it" --
    # so the round must not be tallied until that conversation finishes.
    request = _REQUEST_RE.search(result.text)
    opened = False
    if request:
        used = _exchanges_used(conn, paper["id"], row["review_round"], row["reviewer_index"])
        if used < config.max_review_exchanges():
            _open_exchange(
                conn, paper, task, row, used + 1, request.group(1).strip(), lab_dir, job_id
            )
            opened = True
        # Past the cap the request is simply ignored: the verdict is real
        # and the rubric already told the reviewer this was its last turn.

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

    if not opened:
        _maybe_finalize(conn, paper["id"], row["review_round"], job_id)
    return "done"


def _exchanges_used(conn, paper_id: int, review_round: int, reviewer_index: int) -> int:
    return conn.execute(
        "SELECT COUNT(*) AS n FROM review_exchanges WHERE target_type='paper' "
        "AND target_id=? AND review_round=? AND reviewer_index=?",
        (paper_id, review_round, reviewer_index),
    ).fetchone()["n"]


def _open_exchange(conn, paper, task, row, exchange_round, request_text, lab_dir, job_id) -> int:
    """Record a reviewer's request and queue the authors' answer."""
    base = (f"{task['lab_id']}/tasks/{paper['task_id']}/papers/{paper['id']}"
            f"/reviews/{row['review_round']}")
    relpath = f"{base}/{row['reviewer_index']}.request.{exchange_round}.md"
    write_artifact(lab_dir / relpath, request_text)
    conn.execute(
        "INSERT INTO review_exchanges (target_type, target_id, review_round, reviewer_index, "
        "exchange_round, request_path) VALUES ('paper', ?, ?, ?, ?, ?)",
        (paper["id"], row["review_round"], row["reviewer_index"], exchange_round, relpath),
    )
    cur = conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status, review_round, reviewer_index) "
        "VALUES ('author_response', 'paper', ?, 'pending', ?, ?)",
        (paper["id"], row["review_round"], row["reviewer_index"]),
    )
    record_job_event(
        conn, job_id=job_id, actor_type="reviewer", actor_id=None,
        event_type="review_request_raised", target_type="paper", target_id=paper["id"],
        payload_path=relpath,
    )
    return cur.lastrowid


def exchange_transcript(conn, paper_id: int, review_round: int, reviewer_index: int,
                        lab_dir: Path) -> str:
    """This reviewer's own request/response history, for its next turn.

    Scoped to one reviewer on purpose: showing reviewer 2 what reviewer 1
    asked, or what the authors told reviewer 1, would let the panel
    converge through the authors as a relay and the three verdicts would
    stop being three independent observations.
    """
    rows = conn.execute(
        "SELECT * FROM review_exchanges WHERE target_type='paper' AND target_id=? "
        "AND review_round=? AND reviewer_index=? ORDER BY exchange_round",
        (paper_id, review_round, reviewer_index),
    ).fetchall()
    if not rows:
        return ""
    parts = []
    for r in rows:
        req = (lab_dir / r["request_path"]).read_text(errors="replace") \
            if (lab_dir / r["request_path"]).exists() else "(request missing)"
        parts.append(f"--- You asked (exchange {r['exchange_round']}) ---\n{req}")
        if r["response_path"]:
            path = lab_dir / r["response_path"]
            resp = path.read_text(errors="replace") if path.exists() else "(response missing)"
            parts.append(f"--- The authors answered ---\n{resp}")
    remaining = config.max_review_exchanges() - len(rows)
    closing = (
        "You have no exchanges left: return a VERDICT on what you now have."
        if remaining <= 0 else
        f"You may make {remaining} further request if you genuinely need one."
    )
    return (
        "\n\nYou already corresponded with the authors about this document. "
        "Judge the answers as evidence -- an answer that dodges the question, or a "
        "computation that does not show what you asked for, counts against the paper.\n\n"
        + "\n\n".join(parts)
        + f"\n\n{closing}\n"
    )


def _rejected_paper_count(conn: sqlite3.Connection, task_id: int) -> int:
    """Rejected papers for THIS task only -- unlike the accepted count,
    which is lab-wide. One task failing repeatedly must not be masked by
    other tasks in the same lab succeeding."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM papers WHERE status = 'rejected' AND task_id = ?",
        (task_id,),
    ).fetchone()
    return row["n"]


def _accepted_paper_count(conn: sqlite3.Connection, task_id: int) -> int:
    """Accepted papers across the whole lab that owns `task_id`.

    Lab-wide, not per-task: the target is how much the lab has produced,
    and papers from different tasks all count toward it.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM papers "
        "JOIN tasks ON tasks.id = papers.task_id "
        "WHERE papers.status = 'accepted' AND tasks.lab_id = "
        "(SELECT lab_id FROM tasks WHERE id = ?)",
        (task_id,),
    ).fetchone()
    return row["n"]


def _maybe_finalize(conn: sqlite3.Connection, paper_id: int, review_round: int, job_id: int) -> None:
    """Once all REVIEWER_COUNT reviews for this round are in, tally.

    Pass  -> paper accepted, student back to 'working', task handed to the
             professor for the §3.3 callback decision.
    Fail  -> paper rejected and a student_revise_paper job is enqueued so
             the student revises against the reviewers' objections and
             resubmits (§3.2 step 4) -- unless the lab has already reached
             its accepted-paper target, in which case the paper stays
             rejected and the re-scope/abandon call goes to the professor.
    """
    reported = conn.execute(
        "SELECT COUNT(*) AS n FROM reviews WHERE target_type='paper' AND target_id=? AND review_round=?",
        (paper_id, review_round),
    ).fetchone()["n"]
    if reported < REVIEWER_COUNT:
        return

    # All three have filed a verdict, but one of them may still be talking
    # to the authors -- reviewer 3 can land last while reviewer 1 is
    # mid-exchange. Tallying now would accept or reject the paper on a
    # verdict its own author has already said it might revise, and would
    # leave the returning reviewer writing into a finished round.
    outstanding = conn.execute(
        "SELECT COUNT(*) AS n FROM review_exchanges WHERE target_type='paper' "
        "AND target_id=? AND review_round=? AND response_path IS NULL",
        (paper_id, review_round),
    ).fetchone()["n"]
    pending_turns = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE target_type='paper' AND target_id=? "
        "AND review_round=? AND kind IN ('paper_review','author_response') "
        "AND status IN ('pending','running')",
        (paper_id, review_round),
    ).fetchone()["n"]
    if outstanding or pending_turns:
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
        # Enrol in the shared reference bank so later students -- and
        # later labs -- can cite this result as established work.
        references.register_accepted_paper(conn, paper_id)
        # Hand the task to the professor (§3.3): an accepted paper does
        # not by itself mean the task's question is settled.
        callback.request_callback(conn, paper["task_id"])
        # And ask whether this result belongs with earlier accepted work
        # (§ collaboration): nothing used to notice that two papers held
        # competing results that could not both be tight.
        task_row = conn.execute(
            "SELECT lab_id FROM tasks WHERE id = ?", (paper["task_id"],)
        ).fetchone()
        if task_row:
            collaboration.request_scan(conn, task_row["lab_id"])
    elif _accepted_paper_count(conn, paper["task_id"]) >= config.max_accepted_papers():
        # The lab has what it needs; the revise loop stops here and the
        # professor decides what becomes of the task (§3.3). Without this
        # a rejected paper past the target was simply a dead end.
        callback.request_callback(conn, paper["task_id"])
        # And ask whether this result belongs with earlier accepted work
        # (§ collaboration): nothing used to notice that two papers held
        # competing results that could not both be tight.
        task_row = conn.execute(
            "SELECT lab_id FROM tasks WHERE id = ?", (paper["task_id"],)
        ).fetchone()
        if task_row:
            collaboration.request_scan(conn, task_row["lab_id"])
    elif _rejected_paper_count(conn, paper["task_id"]) >= config.max_rejected_papers():
        # Terminal: this task has spent its attempts. Neither existing cap
        # can stop this case -- max_accepted_papers counts successes and a
        # failing task has none, while the supervision cap forces a
        # write-up whose rejection lands straight back in supervision. The
        # loop that produced 29 rejected papers on task #4 ran through
        # exactly this branch 28 times.
        #
        # Abandon rather than hand it to the professor: the professor has
        # already been consulted every round and kept saying continue, so
        # asking again is the same question that failed to terminate.
        # trg_tasks_release_student frees the student.
        conn.execute("UPDATE tasks SET status = 'abandoned' WHERE id = ?", (paper["task_id"],))
    else:
        # Revise-and-resubmit (§3.2 step 4). Without this a rejected paper
        # is a dead end -- the same gap `lab revise` closed for labs. The
        # loop keeps going until the lab reaches its accepted-paper target;
        # once it has, a further rejection stops here and leaves the
        # re-scope/abandon call to the professor (§3.3).
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('student_revise_paper', 'paper', ?, 'pending')",
            (paper_id,),
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
