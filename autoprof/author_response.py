"""The authors' side of a reviewer's request (docs/DESIGN.md §4).

A reviewer that needs evidence before it can decide raises a REQUEST
instead of a VERDICT; this handler is what answers it. The authors get
tool access, because the requests worth making are overwhelmingly of the
form "run the finite check and show me the output" -- three independent
reviewers withheld strong_accept from a correct paper for exactly that,
and the paper could not have supplied it, because nothing in the system
ever told the authors to run one.

Answering is deliberately NOT revising. The paper under review does not
change: a reviewer asked what the authors already know or can compute,
and gets an answer. Editing the document mid-round would invalidate the
other two reviewers, who are reading the version that was submitted.
"""

import sqlite3
import uuid
from pathlib import Path

from . import jobs, tools
from .artifacts import write_artifact
from .backends.base import Backend
from .events import record_job_event

MAX_TOOL_ROUNDS = 3

RESPONSE_PROMPT_TEMPLATE = """You are the author of the paper below, which is under peer \
review. One reviewer has asked you for something before they will commit to a verdict.

Answer the request. Specifically:

- Answer what was actually asked, directly, at the top. Do not restate the paper.
- If they asked for a computation, RUN IT with the tools below and report the exact output, \
including the search domain and any parameters. A described computation is not a performed one.
- If the answer is unfavourable to your paper, say so plainly. The reviewer will judge the \
answer as evidence, and an answer that dodges counts against you far more than an honest \
negative result. If a check they asked for fails, that is something you needed to know.
- If you cannot supply what they asked, say why, precisely.
- Do NOT rewrite or re-argue the paper. It is under review as submitted and does not change \
here. You are answering a question, not revising.

{tool_docs}

--- THE REVIEWER'S REQUEST ---
{request}

--- YOUR PAPER, AS SUBMITTED ---
{paper}
"""


def execute_author_response_job(
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
    if row["review_round"] != paper["review_round"]:
        # The paper was revised and resubmitted while this sat in backoff.
        # The question was about a version nobody is reviewing any more.
        jobs.complete_job(conn, job_id, lease_id)
        return "done"

    exchange = conn.execute(
        "SELECT * FROM review_exchanges WHERE target_type='paper' AND target_id=? "
        "AND review_round=? AND reviewer_index=? AND response_path IS NULL "
        "ORDER BY exchange_round DESC LIMIT 1",
        (paper["id"], row["review_round"], row["reviewer_index"]),
    ).fetchone()
    if exchange is None:
        jobs.complete_job(conn, job_id, lease_id)
        return "done"

    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (paper["task_id"],)).fetchone()
    paper_file = lab_dir / paper["path"]
    if not paper_file.exists():
        return jobs.fail_job(conn, job_id, lease_id, f"paper file missing: {paper['path']}")
    request_file = lab_dir / exchange["request_path"]

    prompt = RESPONSE_PROMPT_TEMPLATE.format(
        tool_docs=tools.TOOL_DOCS.format(
            timeout=tools.VERIFY_TIMEOUT_SECONDS,
            max_calls=tools.MAX_TOOL_CALLS_PER_ROUND,
            max_series=len(tools.SERIES_COLOURS),
        ),
        request=request_file.read_text(errors="replace") if request_file.exists() else "(missing)",
        paper=paper_file.read_text(errors="replace"),
    )
    result = jobs.run_with_session(conn, job_id, backend, prompt)
    if result.rate_limited:
        jobs.record_rate_limit(
            conn, job_id, lease_id, result.retry_after_seconds, provider=backend.name
        )
        return "rate_limited"
    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)

    # Bounded tool loop, same shape as student_work: the point of this job
    # is that the authors can actually run what was asked.
    transcript = prompt
    for _ in range(MAX_TOOL_ROUNDS):
        calls = tools.parse_tool_calls(result.text)
        if not calls:
            break
        tool_results = tools.execute_tool_calls(
            conn, calls, lab_id=task["lab_id"], task_id=task["id"],
            student_id=paper["student_id"], lab_dir=lab_dir,
        )
        transcript = (
            f"{transcript}\n\n--- your previous response ---\n{result.text}\n\n{tool_results}\n\n"
            "Now write your final answer to the reviewer, incorporating these results. "
            "Report exact outputs, not summaries of them."
        )
        result = jobs.run_with_session(conn, job_id, backend, transcript)
        if result.rate_limited:
            jobs.record_rate_limit(
                conn, job_id, lease_id, result.retry_after_seconds, provider=backend.name
            )
            return "rate_limited"
        if result.is_error:
            return jobs.fail_job(conn, job_id, lease_id, result.error)

    if not (result.text or "").strip():
        return jobs.fail_job(conn, job_id, lease_id, "empty author response")

    relpath = exchange["request_path"].replace(".request.", ".response.")
    write_artifact(lab_dir / relpath, result.text)
    conn.execute(
        "UPDATE review_exchanges SET response_path=? WHERE id=?", (relpath, exchange["id"])
    )

    # Hand the reviewer its turn back, resuming ITS session rather than
    # starting a fresh one: the reviewer already attacked this paper and
    # should continue from that, not re-derive it. The session id lives on
    # the original review job for this reviewer and round.
    prior = conn.execute(
        "SELECT backend_session_id FROM jobs WHERE kind='paper_review' AND target_type='paper' "
        "AND target_id=? AND review_round=? AND reviewer_index=? "
        "ORDER BY id DESC LIMIT 1",
        (paper["id"], row["review_round"], row["reviewer_index"]),
    ).fetchone()
    conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status, review_round, reviewer_index, "
        "backend_session_id) VALUES ('paper_review', 'paper', ?, 'pending', ?, ?, ?)",
        (paper["id"], row["review_round"], row["reviewer_index"],
         prior["backend_session_id"] if prior else None),
    )

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"
    record_job_event(
        conn, job_id=job_id, actor_type="student", actor_id=paper["student_id"],
        event_type="review_request_answered", target_type="paper", target_id=paper["id"],
        payload_path=relpath,
    )
    conn.commit()
    return "done"
