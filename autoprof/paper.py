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

from . import assumptions, db, ingest, jobs, references, supervision, tools
from .artifacts import checkpoint_artifact, write_artifact
from .backends.base import Backend
from .events import record_job_event

_PAPER_TEMPLATE_PATH = db.REPO_ROOT / "templates" / "paper_template.html"
_LEADING_HTML_COMMENT_RE = re.compile(r"^\s*<!--.*?-->\s*\n", re.DOTALL)
_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
MAX_TOOL_ROUNDS = 3

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

{supervision}

{corpus}

{tool_docs}

{assumption_docs}

{ledger}

Now do the actual research work. Think hard and go as far as you can toward an actual \
result -- not a plan for getting one. Specifically:

- If the task direction is "prove" or "disprove", construct the actual argument, in full, \
with every step checkable. State every assumption you rely on.
- Do not assert a result you have not derived. A rigorous negative result, or a precisely \
characterised partial result with the obstruction identified, is worth far more here than \
an overclaimed positive one -- your work will be reviewed by independent reviewers who \
will check each step and reject the paper if any step fails.
- Record dead ends you tried so you don't repeat them.
- If your supervisor gave you guidance above, address it directly and say what you did about \
each point. If you disagree with a point, say so explicitly and explain why -- do not silently \
ignore it.

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

{supervision}

{reference_bank}

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
- Write it as a person would, not as a filled-in template. Give the reader a narrative: \
why the problem matters, the idea of the argument in plain terms BEFORE its formal execution, \
and signposting between sections. Do not write sections that merely restate their own titles.
- Include figures, tables and diagrams wherever the content has shape a reader would otherwise \
have to reconstruct: a function plotted across its parameter range, exact values tabulated, a \
case breakdown, the structure of a counterexample, a comparison against prior bounds. Draw them \
as inline <svg> using the template's figure/table environments, following the colour and axis \
rules in the template's authoring comment. The paper is printed to PDF, so everything must be \
legible in static ink -- no interactivity, and it must survive greyscale.
- Reference every figure and table from the prose and caption it with what the reader should \
take from it. A figure that just repeats the sentence beside it, or a two-row table, is worse \
than nothing -- omit it. Reviewers judge whether each visual earns its space.

Respond with ONLY the HTML document. No markdown code fences, no commentary before or \
after it.
"""


REVISE_PROMPT_TEMPLATE = """You are a PhD student revising a paper that was REJECTED in peer review.

Your lab's root problem:
<root_problem>
{root_problem}
</root_problem>

Your task: "{title}" (direction: {direction})
End criteria: {end_criteria}

Your working memory from the research:
<memory>
{memory}
</memory>

This is the paper as submitted:
<paper>
{paper}
</paper>

{reference_bank}

The independent reviewers said the following. They did not see each other's reviews, so where \
two of them raise the same point independently, treat it as certainly real:
<reviews>
{reviews}
</reviews>

Produce a revised version of the paper that addresses every reviewer objection. Specifically:

- Fix every factual and bibliographic error they identify. If a reviewer says a citation has the \
wrong title or is missing, correct it to the real work -- do NOT invent a plausible-looking \
reference, and do not cite anything you cannot vouch for. If you cannot verify a citation, \
remove the claim that depends on it or state it as an assumption.
- Where a reviewer says a claim is unsupported or an attribution is untraceable, either support \
it properly or explicitly label it as an assumption inherited from the problem statement.
- Strengthen Related Work to honestly position the contribution against the prior art the \
reviewers named. Do not overclaim novelty.
- Do NOT weaken, overstate, or quietly change the mathematical results to make them look better. \
If a reviewer found a genuine mathematical error, fix the mathematics and say so plainly. If the \
reviewers agreed the mathematics is correct, keep it as it is.
- Address scope criticism by stating precisely what is and is not resolved, rather than by \
claiming more than you proved.
- Write it as a person would, not as a filled-in template. Give the reader a narrative: \
why the problem matters, the idea of the argument in plain terms BEFORE its formal execution, \
and signposting between sections. Do not write sections that merely restate their own titles.
- Include figures, tables and diagrams wherever the content has shape a reader would otherwise \
have to reconstruct: a function plotted across its parameter range, exact values tabulated, a \
case breakdown, the structure of a counterexample, a comparison against prior bounds. Draw them \
as inline <svg> using the template's figure/table environments, following the colour and axis \
rules in the template's authoring comment. The paper is printed to PDF, so everything must be \
legible in static ink -- no interactivity, and it must survive greyscale.
- Reference every figure and table from the prose and caption it with what the reader should \
take from it. A figure that just repeats the sentence beside it, or a two-row table, is worse \
than nothing -- omit it. Reviewers judge whether each visual earns its space.

Return the COMPLETE revised paper as a single self-contained HTML document in exactly the same \
format and structure as the version above. No markdown code fences, no commentary before or \
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

    work_prompt = WORK_PROMPT_TEMPLATE.format(
            root_problem=lab["root_problem"],
            brief=brief,
            title=task["title"],
            direction=task["direction"],
            end_criteria=task["end_criteria"],
            memory=memory,
            supervision=supervision.render_student_guidance(conn, task["id"], lab_dir),
            corpus=ingest.render_corpus(conn, lab["id"], lab_dir),
            tool_docs=tools.TOOL_DOCS.format(
                timeout=tools.VERIFY_TIMEOUT_SECONDS,
                max_calls=tools.MAX_TOOL_CALLS_PER_ROUND,
                max_series=len(tools.SERIES_COLOURS),
            ),
            assumption_docs=assumptions.ASSUMPTION_DOCS,
            ledger=assumptions.render(conn, task["id"]),
    )
    result = jobs.run_with_session(conn, job_id, backend, work_prompt)

    if result.rate_limited:
        jobs.record_rate_limit(
            conn, job_id, lease_id, result.retry_after_seconds, provider=backend.name
        )
        return "rate_limited"
    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)

    # Bounded tool loop: run whatever the student called, hand the results
    # back, let them revise. Capped at MAX_TOOL_ROUNDS so a student cannot
    # spend a job cycling on tools instead of producing work.
    prompt_so_far = work_prompt
    for _ in range(MAX_TOOL_ROUNDS):
        calls = tools.parse_tool_calls(result.text)
        if not calls:
            break
        tool_results = tools.execute_tool_calls(
            conn,
            calls,
            lab_id=lab["id"],
            task_id=task["id"],
            student_id=student["id"],
            lab_dir=lab_dir,
        )
        prompt_so_far = (
            f"{prompt_so_far}\n\n--- your previous response ---\n{result.text}\n\n"
            f"{tool_results}\n\nNow produce your complete updated working memory, taking the "
            "tool results into account. You may call tools again if you genuinely need to."
        )
        follow_up = jobs.run_with_session(conn, job_id, backend, prompt_so_far)
        if follow_up.rate_limited:
            jobs.record_rate_limit(conn, job_id, lease_id, follow_up.retry_after_seconds)
            return "rate_limited"
        if follow_up.is_error or not follow_up.text.strip():
            # Keep the pre-tool response rather than losing the work.
            break
        result = follow_up

    # Belt and braces alongside the backend's own empty-output check:
    # memory.md is overwritten wholesale, so writing an empty result would
    # destroy everything the student has established so far and leave the
    # write-up job nothing to work from. Fail the job instead -- the
    # research is recoverable by retry, an erased memory is not.
    if not result.text.strip():
        return jobs.fail_job(
            conn, job_id, lease_id, "backend returned empty work output; refusing to erase memory"
        )

    # §7: snapshot before overwriting. memory.md is replaced wholesale
    # each pass, so without a checkpoint one bad write is unrecoverable.
    memory_file = lab_dir / student["memory_path"]
    checkpoint_artifact(memory_file)
    assumptions.record(
        conn,
        assumptions.parse_blocks(result.text),
        lab_id=lab["id"],
        task_id=task["id"],
        student_id=student["id"],
    )
    write_artifact(memory_file, result.text)
    # Report to the supervisor rather than writing up immediately. The
    # professor decides whether this is ready (docs/DESIGN.md §3.2, and
    # autoprof/supervision.py) -- catching "not actually proved yet" here
    # costs one job, catching it at peer review costs three reviews.
    conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status) "
        "VALUES ('professor_supervision', 'task', ?, 'pending')",
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

    result = jobs.run_with_session(
        conn,
        job_id,
        backend,
        PAPER_PROMPT_TEMPLATE.format(
            root_problem=lab["root_problem"],
            title=task["title"],
            direction=task["direction"],
            end_criteria=task["end_criteria"],
            memory=memory,
            supervision=supervision.render_student_guidance(conn, task["id"], lab_dir),
            reference_bank=references.render_for_prompt(conn),
            template=template,
        )
    )

    if result.rate_limited:
        jobs.record_rate_limit(
            conn, job_id, lease_id, result.retry_after_seconds, provider=backend.name
        )
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


def execute_student_revise_paper_job(
    conn: sqlite3.Connection, job_id: int, backend: Backend, lab_dir: Path
) -> str:
    """Revise a rejected paper against its reviewers' objections, then
    resubmit it for a fresh round.

    docs/DESIGN.md §3.2 step 4. Without this a rejected paper was a dead
    end -- exactly the gap `lab revise` closed at the lab level. The
    revision overwrites the paper in place and the round is bumped by
    `resubmit_paper`, so every round's reviews stay addressable by round
    number while the paper itself is a single evolving document.

    Targets the PAPER, not the task: a task can accumulate several papers
    over its life, and it is one specific rejected paper being revised.
    """
    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=3600):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    paper = conn.execute("SELECT * FROM papers WHERE id = ?", (row["target_id"],)).fetchone()
    if paper is None:
        return jobs.fail_job(conn, job_id, lease_id, f"no paper with id={row['target_id']}")
    if paper["status"] != "rejected":
        # Not an error worth retrying: the paper was already resubmitted or
        # accepted by another path, so this job has nothing left to do.
        jobs.complete_job(conn, job_id, lease_id)
        return "done"

    try:
        task, student, lab, memory, _brief = _load_context(conn, paper["task_id"], lab_dir)
    except PaperError as e:
        return jobs.fail_job(conn, job_id, lease_id, str(e))

    paper_file = lab_dir / paper["path"]
    if not paper_file.exists():
        return jobs.fail_job(conn, job_id, lease_id, f"paper file missing: {paper['path']}")

    review_rows = conn.execute(
        "SELECT * FROM reviews WHERE target_type='paper' AND target_id=? AND review_round=? "
        "ORDER BY reviewer_index",
        (paper["id"], paper["review_round"]),
    ).fetchall()
    if not review_rows:
        return jobs.fail_job(
            conn, job_id, lease_id, f"paper {paper['id']} has no reviews to revise against"
        )

    reviews = []
    for review in review_rows:
        rationale_file = lab_dir / review["rationale_path"]
        body = rationale_file.read_text() if rationale_file.exists() else "(rationale missing)"
        reviews.append(f"--- Reviewer {review['reviewer_index']} ({review['verdict']}) ---\n{body}")

    result = jobs.run_with_session(
        conn,
        job_id,
        backend,
        REVISE_PROMPT_TEMPLATE.format(
            root_problem=lab["root_problem"],
            title=task["title"],
            direction=task["direction"],
            end_criteria=task["end_criteria"],
            memory=memory,
            reference_bank=references.render_for_prompt(conn),
            paper=paper_file.read_text(),
            reviews="\n\n".join(reviews),
        ),
    )

    if result.rate_limited:
        jobs.record_rate_limit(
            conn, job_id, lease_id, result.retry_after_seconds, provider=backend.name
        )
        return "rate_limited"
    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)

    html = _strip_fence(result.text)
    if "<h1" not in html.lower():
        return jobs.fail_job(
            conn, job_id, lease_id, f"revision is not an HTML document: {html[:300]}"
        )

    write_artifact(lab_dir / paper["path"], html)
    conn.execute(
        "UPDATE papers SET title = ? WHERE id = ?",
        (extract_title(html, task["title"]), paper["id"]),
    )
    conn.execute("UPDATE students SET status = 'in_review' WHERE id = ?", (student["id"],))

    from .paper_review import resubmit_paper

    resubmit_paper(conn, paper["id"])

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"

    record_job_event(
        conn,
        job_id=job_id,
        actor_type="student",
        actor_id=student["id"],
        event_type="paper_revised",
        target_type="paper",
        target_id=paper["id"],
        payload_path=paper["path"],
    )
    conn.commit()
    return "done"


_TEMPLATE_RULES = """Rules:
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
- Write it as a person would, not as a filled-in template. Give the reader a narrative: \
why the problem matters, the idea of the argument in plain terms BEFORE its formal execution, \
and signposting between sections. Do not write sections that merely restate their own titles.
- Include figures, tables and diagrams wherever the content has shape a reader would otherwise \
have to reconstruct: a function plotted across its parameter range, exact values tabulated, a \
case breakdown, the structure of a counterexample, a comparison against prior bounds. Draw them \
as inline <svg> using the template's figure/table environments, following the colour and axis \
rules in the template's authoring comment. The paper is printed to PDF, so everything must be \
legible in static ink -- no interactivity, and it must survive greyscale.
- Reference every figure and table from the prose and caption it with what the reader should \
take from it. A figure that just repeats the sentence beside it, or a two-row table, is worse \
than nothing -- omit it. Reviewers judge whether each visual earns its space.
"""


COLLAB_PAPER_PROMPT_TEMPLATE = """You are writing up a JOINT paper with several co-authors, \
based on the collaboration's agreed shared state.

Your lab's root problem:
<root_problem>
{root_problem}
</root_problem>

Goal of the collaboration:
<goal>
{goal}
</goal>

The agreed shared state of the joint work -- this is what the paper must present:
<shared_state>
{shared}
</shared_state>

{reference_bank}

This paper has {n_authors} authors. Write it as one voice, not as stitched-together sections: \
a single narrative with consistent notation throughout. It must contain a unifying result that \
none of the individual contributions had on its own -- if the paper reads as three separate \
results placed side by side, it has failed.

{template_rules}

<template>
{template}
</template>

Respond with ONLY the HTML document. No markdown code fences, no commentary.
"""


def execute_collaboration_write_paper_job(
    conn: sqlite3.Connection, job_id: int, backend: Backend, lab_dir: Path
) -> str:
    """Write the joint paper and record every author on it."""
    from . import collaboration

    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=3600):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    collab = conn.execute(
        "SELECT * FROM collaborations WHERE task_id = ?", (row["target_id"],)
    ).fetchone()
    if collab is None:
        return jobs.fail_job(conn, job_id, lease_id, f"no collaboration on task {row['target_id']}")

    try:
        task, student, lab, _memory, _brief = _load_context(conn, collab["task_id"], lab_dir)
    except PaperError as e:
        return jobs.fail_job(conn, job_id, lease_id, str(e))

    live = conn.execute(
        "SELECT COUNT(*) AS n FROM papers WHERE task_id = ? AND status IN ('draft', 'in_review')",
        (task["id"],),
    ).fetchone()["n"]
    if live > 0:
        jobs.complete_job(conn, job_id, lease_id)
        return "done"

    shared_file = lab_dir / collab["memory_path"]
    if not shared_file.exists():
        return jobs.fail_job(conn, job_id, lease_id, f"shared state missing: {collab['memory_path']}")

    template = _LEADING_HTML_COMMENT_RE.sub("", _PAPER_TEMPLATE_PATH.read_text(), count=1)
    members = conn.execute(
        "SELECT COUNT(*) AS n FROM collaboration_members WHERE collaboration_id = ?",
        (collab["id"],),
    ).fetchone()["n"]

    result = jobs.run_with_session(
        conn,
        job_id,
        backend,
        COLLAB_PAPER_PROMPT_TEMPLATE.format(
            root_problem=lab["root_problem"],
            goal=collab["goal"],
            shared=shared_file.read_text(),
            n_authors=members,
            reference_bank=references.render_for_prompt(conn),
            template_rules=_TEMPLATE_RULES,
            template=template,
        ),
    )

    if result.rate_limited:
        jobs.record_rate_limit(
            conn, job_id, lease_id, result.retry_after_seconds, provider=backend.name
        )
        return "rate_limited"
    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)

    html = _strip_fence(result.text)
    if "<h1" not in html.lower():
        return jobs.fail_job(
            conn, job_id, lease_id, f"joint paper output is not an HTML document: {html[:300]}"
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

    authors = collaboration.record_authors(conn, paper_id, collab["id"])
    conn.execute("UPDATE collaborations SET status='concluded' WHERE id=?", (collab["id"],))
    for author_id in authors:
        conn.execute("UPDATE students SET status='in_review' WHERE id=?", (author_id,))

    from .paper_review import request_paper_review

    request_paper_review(conn, paper_id)

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"

    record_job_event(
        conn,
        job_id=job_id,
        actor_type="student",
        actor_id=student["id"],
        event_type="joint_paper_submitted",
        target_type="paper",
        target_id=paper_id,
        payload_path=relpath,
    )
    conn.commit()
    return "done"
