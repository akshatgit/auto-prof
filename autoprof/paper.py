"""The student half of the work loop: work the task, then write the paper.

docs/DESIGN.md §3.2 steps 1-2, split across two job kinds rather than one:

  student_work        -- work the problem, update memory.md, then enqueue
  student_write_paper -- draft the actual paper from that memory, create
                         the `papers` row, and request review

They're separate jobs because they fail differently and are worth
retrying separately: "the model couldn't make progress on the maths" and
"the model produced a paper with no VERDICT-able structure" are not the
same failure, and collapsing them would mean a formatting failure throws
away the research work that preceded it.
"""

import re
import sqlite3
import uuid
from pathlib import Path

from . import db, jobs
from .artifacts import write_artifact
from .backends.base import Backend
from .events import record_job_event

_PAPER_TEMPLATE_PATH = db.REPO_ROOT / "templates" / "paper_template.html"
_LEADING_HTML_COMMENT_RE = re.compile(r"^\s*<!--.*?-->\s*\n", re.DOTALL)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_FENCE_RE = re.compile(r"^```(?:html)?\s*(.*?)\s*```$", re.DOTALL)

WORK_PROMPT_TEMPLATE = """You are a PhD student in a research lab. You are working on one task.

Your lab's root problem:
<root_problem>
{root_problem}
</root_problem>

Your task brief (written by your professor):
<task_brief>
{brief}
</task_brief>

Task: "{title}"
Direction you were asked to attempt: {direction}
End criteria: {end_criteria}

Your working memory so far:
<memory>
{memory}
</memory>

Now do the actual research work. Think hard and go as far as you can toward an actual \
result -- not a plan for getting one. Specifically:

- If the task direction is "prove" or "disprove", construct the actual argument, in full, \
with every step checkable. State every assumption you rely on.
- Do not assert a result you have not derived. A rigorous negative result, or a precisely \
characterised partial result with the obstruction identified, is worth far more here than \
an overclaimed positive one -- your work will be reviewed by independent reviewers who \
will check each step and reject the paper if any step fails.
- Record dead ends you tried so you don't repeat them.

Write your response as your complete updated working memory: current status, the full \
derivation or argument as it stands, what remains open, dead ends, and your next step.
"""

PAPER_PROMPT_TEMPLATE = """You are a PhD student writing up a completed piece of research \
as a paper for peer review.

Your lab's root problem:
<root_problem>
{root_problem}
</root_problem>

Your task: "{title}" (direction: {direction})
End criteria: {end_criteria}

Your complete working memory from doing the research:
<memory>
{memory}
</memory>

Write the paper as a single self-contained HTML document, following the template below \
EXACTLY -- same structure, same CSS, same section order, same class names. The template's \
ACM two-column layout, section numbering, and theorem numbering are driven by CSS \
counters, so never type section or theorem numbers by hand.

<template>
{template}
</template>

Rules:
- Replace every {{placeholder}} and remove every class="fillme" marker -- an unfilled \
placeholder left in the document is an automatic reject at review.
- Remove the template's leading HTML authoring comment from your output.
- Section 4 (Result) MUST state explicitly whether this is a PROOF, a DISPROOF, or another \
kind of result. Reviewers check this first.
- Section 4 must contain the complete, checkable argument -- every step, every assumption. \
Not a sketch. Number lemmas and steps using the provided environments so a reviewer can \
point at exactly the step they disagree with.
- Related Work must be honest about what was already known. Reviewers assess novelty \
against exactly that section, and overclaiming there is the fastest way to be rejected.
- Discussion & Limitations must honestly state what the result does NOT show.
- Do not claim a stronger result than your working memory actually supports.

Respond with ONLY the HTML document. No markdown code fences, no commentary before or \
after it.
"""


class PaperError(RuntimeError):
    pass


def _strip_fence(text: str) -> str:
    text = text.strip()
    match = _FENCE_RE.match(text)
    return match.group(1).strip() if match else text


def extract_title(html: str, fallback: str) -> str:
    """Pull a display title out of the generated paper.

    <title> first, then <h1>; both get their inner tags stripped since the
    template's h1 can legitimately contain markup. Falls back to the task
    title so a papers row is never blocked on cosmetics.
    """
    for pattern in (_TITLE_RE, _H1_RE):
        match = pattern.search(html)
        if match:
            title = _TAG_RE.sub("", match.group(1)).strip()
            if title and "{" not in title:
                return title[:300]
    return fallback


def _load_context(conn, task_id: int, lab_dir: Path):
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        raise PaperError(f"no task with id={task_id}")
    if task["assigned_student_id"] is None:
        raise PaperError(f"task {task_id} has no assigned student")

    student = conn.execute(
        "SELECT * FROM students WHERE id = ?", (task["assigned_student_id"],)
    ).fetchone()
    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (task["lab_id"],)).fetchone()

    memory_file = lab_dir / student["memory_path"]
    memory = memory_file.read_text() if memory_file.exists() else "(no memory recorded yet)"

    brief_file = lab_dir / task["brief_path"]
    brief = brief_file.read_text() if brief_file.exists() else "(no brief written)"

    return task, student, lab, memory, brief


def execute_student_work_job(
    conn: sqlite3.Connection, job_id: int, backend: Backend, lab_dir: Path
) -> str:
    """Work the task, update memory, then enqueue the write-up job."""
    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=1800):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    try:
        task, student, lab, memory, brief = _load_context(conn, row["target_id"], lab_dir)
    except PaperError as e:
        return jobs.fail_job(conn, job_id, lease_id, str(e))

    if student["paused_at"] is not None:
        # Human pause (docs/TASKS.md Phase 3) is orthogonal to status:
        # release the lease and leave the job pending rather than burning
        # an attempt on a student the human has deliberately stopped.
        conn.execute(
            "UPDATE jobs SET status='pending', lease_id=NULL, lease_expires_at=NULL WHERE id=?",
            (job_id,),
        )
        conn.commit()
        return "not_claimed"

    result = backend.run(
        WORK_PROMPT_TEMPLATE.format(
            root_problem=lab["root_problem"],
            brief=brief,
            title=task["title"],
            direction=task["direction"],
            end_criteria=task["end_criteria"],
            memory=memory,
        )
    )

    if result.rate_limited:
        jobs.record_rate_limit(conn, job_id, lease_id, result.retry_after_seconds)
        return "rate_limited"
    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)

    write_artifact(lab_dir / student["memory_path"], result.text)
    conn.execute("UPDATE students SET status = 'writing_paper' WHERE id = ?", (student["id"],))
    conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status) "
        "VALUES ('student_write_paper', 'task', ?, 'pending')",
        (task["id"],),
    )

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"

    record_job_event(
        conn,
        job_id=job_id,
        actor_type="student",
        actor_id=student["id"],
        event_type="student_worked",
        target_type="task",
        target_id=task["id"],
        payload_path=student["memory_path"],
    )
    conn.commit()
    return "done"


def execute_student_write_paper_job(
    conn: sqlite3.Connection, job_id: int, backend: Backend, lab_dir: Path
) -> str:
    """Draft the paper, create the `papers` row, request review."""
    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=1800):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    try:
        task, student, lab, memory, _brief = _load_context(conn, row["target_id"], lab_dir)
    except PaperError as e:
        return jobs.fail_job(conn, job_id, lease_id, str(e))

    # One live paper per task at a time -- a paper already awaiting review
    # means this job is a duplicate (retry after partial application, or a
    # manual re-enqueue), and writing a second would split the review
    # tally across two rows.
    live = conn.execute(
        "SELECT COUNT(*) AS n FROM papers WHERE task_id = ? AND status IN ('draft', 'in_review')",
        (task["id"],),
    ).fetchone()["n"]
    if live > 0:
        jobs.complete_job(conn, job_id, lease_id)
        return "done"

    template = _LEADING_HTML_COMMENT_RE.sub("", _PAPER_TEMPLATE_PATH.read_text(), count=1)

    result = backend.run(
        PAPER_PROMPT_TEMPLATE.format(
            root_problem=lab["root_problem"],
            title=task["title"],
            direction=task["direction"],
            end_criteria=task["end_criteria"],
            memory=memory,
            template=template,
        )
    )

    if result.rate_limited:
        jobs.record_rate_limit(conn, job_id, lease_id, result.retry_after_seconds)
        return "rate_limited"
    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)

    html = _strip_fence(result.text)
    if "<h1" not in html.lower():
        return jobs.fail_job(
            conn, job_id, lease_id, f"paper output is not an HTML document: {html[:300]}"
        )

    cur = conn.execute(
        "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
        "VALUES (?, ?, 'pending', ?, 'in_review', 1)",
        (task["id"], student["id"], extract_title(html, task["title"])),
    )
    paper_id = cur.lastrowid

    relpath = f"{lab['id']}/tasks/{task['id']}/papers/{paper_id}/paper.html"
    conn.execute("UPDATE papers SET path = ? WHERE id = ?", (relpath, paper_id))
    write_artifact(lab_dir / relpath, html)

    conn.execute("UPDATE students SET status = 'in_review' WHERE id = ?", (student["id"],))

    # Imported here rather than at module scope: paper_review imports this
    # module for nothing, but keeping the dependency one-directional at
    # import time avoids a cycle if that ever changes.
    from .paper_review import request_paper_review

    request_paper_review(conn, paper_id)

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"

    record_job_event(
        conn,
        job_id=job_id,
        actor_type="student",
        actor_id=student["id"],
        event_type="paper_submitted",
        target_type="paper",
        target_id=paper_id,
        payload_path=relpath,
    )
    conn.commit()
    return "done"
