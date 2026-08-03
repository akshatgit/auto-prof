"""Multi-student collaboration: several students, one paper.

The lab could supervise a student over a long horizon but had no way for
students to work with *each other*. Every task ran one student to one
paper, so three results that plainly belonged in one paper stayed three
papers -- each individually correct and each criticised as narrow.

The loop mirrors supervision, because the same shape works:

    collaboration_round  (one job per member, in parallel)
            |
            v
    collaboration_synthesis  (professor merges + judges)
            |
            +- continue -> another round
            +- ready    -> collaboration_write_paper (multi-author)
            +- abandon  -> collaboration abandoned

What makes it collaboration rather than concatenation: each member reads
the shared memory AND their co-authors' contributions from the previous
round, and is explicitly asked to engage with them -- reconcile competing
lemmas, adopt a stronger formulation over their own, say so when they
disagree. Contributions are stored per member per round so a disagreement
stays attributable instead of being silently merged away.
"""

import json
import sqlite3
import uuid
from pathlib import Path

from . import config, jobs
from .artifacts import checkpoint_artifact, write_artifact
from .backends.base import Backend
from .events import record_job_event
from .jsonio import extract_json_object

VALID_VERDICTS = ("continue", "ready", "abandon")

CONTRIBUTION_PROMPT_TEMPLATE = """You are a PhD student collaborating with other students in \
your lab on a single joint paper. You are not writing your own paper -- you are contributing to \
one shared piece of work.

Your lab's root problem:
<root_problem>
{root_problem}
</root_problem>

The goal of this collaboration:
<goal>
{goal}
</goal>

This is collaboration round {round}.

Your own established results, from your individual work:
<your_own_work>
{own_memory}
</your_own_work>

{shared}

{others}

Write your contribution to this round. You are expected to ENGAGE with your co-authors, not \
work beside them:

- Where a co-author's result overlaps yours, say explicitly which formulation is stronger and \
why. If theirs subsumes yours, say so plainly and adopt it -- do not defend your own version \
for the sake of authorship.
- Where two contributions conflict, name the conflict precisely and propose how to settle it. \
Do not paper over a disagreement by stating both.
- Where a co-author's lemma lets you strengthen your own result, do that and show the \
derivation.
- Identify what the combined work still lacks to be one coherent paper rather than three \
results in a trench coat: a unifying theorem, consistent notation, a single narrative.
- Do not restate your own prior work at length. Assume your co-authors have read it; contribute \
what is new or what changes in light of theirs.

Respond with your contribution as prose and mathematics. No JSON, no preamble.
"""

SYNTHESIS_PROMPT_TEMPLATE = """You are {name}, a professor in {field}, supervising a \
collaboration between several of your students on one joint paper.

Your lab's root problem:
<root_problem>
{root_problem}
</root_problem>

Goal of the collaboration:
<goal>
{goal}
</goal>

This is synthesis after collaboration round {round}.

{shared}

The contributions from this round, one per student:
<contributions>
{contributions}
</contributions>

Your job is to merge these into a single coherent shared state and decide what happens next.

Assess honestly:
- Has a genuinely unified result emerged, or is this still separate results placed side by side?
  A joint paper must contain something that none of the individual results contained.
- Are there unresolved conflicts between contributions? Name them. Do not average them away.
- Is the notation consistent across contributions? Divergent notation is the usual reason a
  combined paper reads as stapled-together.
- Is any contributor's result now redundant -- subsumed by a stronger one? Say so; a joint paper
  should not carry three proofs of the same thing.
- Would this be a stronger paper than the individual papers were separately? If not, the honest
  answer may be that these results should stay apart.

Then decide one of:
- "continue": another round is warranted. Give each student specific instructions -- name who
  should resolve what.
- "ready": the combined work is a single coherent result and should be written up.
- "abandon": these results do not belong in one paper. Say why.

Respond with ONLY a JSON object, no markdown fences, no commentary, in exactly this shape:
{{"verdict": "continue|ready|abandon", "shared_memory": "...", "guidance": "..."}}
where "shared_memory" is the COMPLETE merged joint state -- it replaces the previous shared
memory, so it must carry everything still relevant, in consistent notation -- and "guidance" is
what the collaborators should do next.
"""


class CollaborationError(RuntimeError):
    pass


def form_collaboration(
    conn: sqlite3.Connection,
    task_id: int,
    student_ids: list[int],
    goal: str,
    lab_dir: Path,
) -> int:
    """Create a collaboration anchored to `task_id`.

    The anchor task's assigned student becomes the lead author (they are
    the one papers.student_id must name, per trg_papers_student_assigned);
    the rest join as co-authors. Their individual task assignments are
    left untouched -- a co-author keeps their own task and memory, and
    contributes to the joint work alongside it.
    """
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        raise CollaborationError(f"no task with id={task_id}")
    lead_id = task["assigned_student_id"]
    if lead_id is None:
        raise CollaborationError(f"task {task_id} has no assigned student to lead the work")
    if lead_id not in student_ids:
        raise CollaborationError(
            f"task {task_id}'s assigned student {lead_id} must be among the collaborators "
            "(they are the lead author)"
        )
    if len(set(student_ids)) < 2:
        raise CollaborationError("a collaboration needs at least two students")

    existing = conn.execute(
        "SELECT id FROM collaborations WHERE task_id = ?", (task_id,)
    ).fetchone()
    if existing:
        raise CollaborationError(f"task {task_id} already anchors collaboration {existing['id']}")

    cur = conn.execute(
        "INSERT INTO collaborations (lab_id, task_id, goal, status, memory_path) "
        "VALUES (?, ?, ?, 'working', 'pending')",
        (task["lab_id"], task_id, goal.strip()),
    )
    collab_id = cur.lastrowid
    relpath = f"{task['lab_id']}/collaborations/{collab_id}/memory.md"
    conn.execute("UPDATE collaborations SET memory_path = ? WHERE id = ?", (relpath, collab_id))

    conn.execute(
        "INSERT INTO collaboration_members (collaboration_id, student_id, role) VALUES (?, ?, 'lead')",
        (collab_id, lead_id),
    )
    for student_id in student_ids:
        if student_id != lead_id:
            conn.execute(
                "INSERT INTO collaboration_members (collaboration_id, student_id, role) "
                "VALUES (?, ?, 'co')",
                (collab_id, student_id),
            )

    write_artifact(
        lab_dir / relpath,
        f"# Collaboration {collab_id}\n\n## Goal\n\n{goal.strip()}\n\n"
        f"## Shared state\n\nNo joint work yet. Round 0.\n",
    )
    _enqueue_round(conn, collab_id)
    conn.commit()
    return collab_id


def _members(conn: sqlite3.Connection, collab_id: int):
    return conn.execute(
        "SELECT * FROM collaboration_members WHERE collaboration_id = ? "
        "ORDER BY CASE role WHEN 'lead' THEN 0 ELSE 1 END, student_id",
        (collab_id,),
    ).fetchall()


def _enqueue_round(conn: sqlite3.Connection, collab_id: int) -> int:
    """Start the next round: one contribution job per member.

    The round counter advances here, before the jobs exist, so every job
    in the round agrees on which round it belongs to even if they are
    dispatched minutes apart.
    """
    conn.execute("UPDATE collaborations SET round = round + 1 WHERE id = ?", (collab_id,))
    round_ = conn.execute(
        "SELECT round FROM collaborations WHERE id = ?", (collab_id,)
    ).fetchone()["round"]
    for member in _members(conn, collab_id):
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status, review_round, reviewer_index) "
            "VALUES ('collaboration_round', 'task', ?, 'pending', ?, ?)",
            (
                conn.execute(
                    "SELECT task_id FROM collaborations WHERE id = ?", (collab_id,)
                ).fetchone()["task_id"],
                round_,
                member["student_id"],
            ),
        )
    return round_


def render_shared(conn: sqlite3.Connection, collab_id: int, lab_dir: Path) -> str:
    row = conn.execute("SELECT memory_path FROM collaborations WHERE id = ?", (collab_id,)).fetchone()
    path = lab_dir / row["memory_path"]
    body = path.read_text() if path.exists() else "(no shared state yet)"
    return f"The collaboration's shared working state:\n<shared_state>\n{body}\n</shared_state>"


def render_others(
    conn: sqlite3.Connection, collab_id: int, student_id: int, round_: int, lab_dir: Path
) -> str:
    """Co-authors' contributions from the PREVIOUS round.

    Previous, not current: contributions within a round are written in
    parallel, so the current round's are not all available yet. Reading
    last round's is what makes each round a genuine exchange rather than
    parallel monologue.
    """
    if round_ <= 1:
        return "This is the first round; your co-authors have not contributed yet."
    rows = conn.execute(
        "SELECT * FROM collaboration_contributions WHERE collaboration_id = ? AND round = ? "
        "AND student_id != ? ORDER BY student_id",
        (collab_id, round_ - 1, student_id),
    ).fetchall()
    if not rows:
        return "Your co-authors did not contribute in the previous round."
    parts = []
    for row in rows:
        path = lab_dir / row["path"]
        body = path.read_text() if path.exists() else "(contribution missing)"
        parts.append(f"--- Student {row['student_id']}, round {row['round']} ---\n{body}")
    return (
        "Your co-authors' contributions from the previous round. Engage with these directly:\n"
        "<co_author_contributions>\n" + "\n\n".join(parts) + "\n</co_author_contributions>"
    )


def execute_collaboration_round_job(
    conn: sqlite3.Connection, job_id: int, backend: Backend, lab_dir: Path
) -> str:
    """One member's contribution to one round."""
    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=3600):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    collab = conn.execute(
        "SELECT * FROM collaborations WHERE task_id = ?", (row["target_id"],)
    ).fetchone()
    if collab is None:
        return jobs.fail_job(conn, job_id, lease_id, f"no collaboration on task {row['target_id']}")
    if collab["status"] != "working":
        jobs.complete_job(conn, job_id, lease_id)
        return "done"

    round_ = row["review_round"]
    student_id = row["reviewer_index"]  # reused as the member slot for this job kind
    student = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if student is None:
        return jobs.fail_job(conn, job_id, lease_id, f"no student with id={student_id}")
    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (collab["lab_id"],)).fetchone()

    own_path = lab_dir / student["memory_path"]
    own_memory = own_path.read_text() if own_path.exists() else "(no individual work recorded)"

    result = jobs.run_with_session(
        conn,
        job_id,
        backend,
        CONTRIBUTION_PROMPT_TEMPLATE.format(
            root_problem=lab["root_problem"],
            goal=collab["goal"],
            round=round_,
            own_memory=own_memory,
            shared=render_shared(conn, collab["id"], lab_dir),
            others=render_others(conn, collab["id"], student_id, round_, lab_dir),
        ),
    )

    if result.rate_limited:
        jobs.record_rate_limit(
            conn, job_id, lease_id, result.retry_after_seconds, provider=backend.name
        )
        return "rate_limited"
    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)
    if not result.text.strip():
        return jobs.fail_job(conn, job_id, lease_id, "empty collaboration contribution")

    relpath = (
        f"{collab['lab_id']}/collaborations/{collab['id']}/rounds/{round_}/{student_id}.md"
    )
    write_artifact(lab_dir / relpath, result.text)
    conn.execute(
        "INSERT OR REPLACE INTO collaboration_contributions "
        "(collaboration_id, student_id, round, path) VALUES (?, ?, ?, ?)",
        (collab["id"], student_id, round_, relpath),
    )

    # The last member to report triggers synthesis. Counting rows rather
    # than tracking a separate flag means a retried job cannot double-fire
    # it: the row is INSERT OR REPLACE, so the count is stable.
    reported = conn.execute(
        "SELECT COUNT(*) AS n FROM collaboration_contributions "
        "WHERE collaboration_id = ? AND round = ?",
        (collab["id"], round_),
    ).fetchone()["n"]
    expected = len(_members(conn, collab["id"]))
    if reported >= expected:
        already = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE kind='collaboration_synthesis' "
            "AND target_id=? AND review_round=?",
            (collab["task_id"], round_),
        ).fetchone()["n"]
        if not already:
            conn.execute(
                "INSERT INTO jobs (kind, target_type, target_id, status, review_round) "
                "VALUES ('collaboration_synthesis', 'task', ?, 'pending', ?)",
                (collab["task_id"], round_),
            )

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"

    record_job_event(
        conn,
        job_id=job_id,
        actor_type="student",
        actor_id=student_id,
        event_type="collaboration_contributed",
        target_type="task",
        target_id=collab["task_id"],
        payload_path=relpath,
    )
    conn.commit()
    return "done"


def execute_collaboration_synthesis_job(
    conn: sqlite3.Connection, job_id: int, backend: Backend, lab_dir: Path
) -> str:
    """Professor merges the round's contributions and decides what next."""
    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=3600):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    collab = conn.execute(
        "SELECT * FROM collaborations WHERE task_id = ?", (row["target_id"],)
    ).fetchone()
    if collab is None:
        return jobs.fail_job(conn, job_id, lease_id, f"no collaboration on task {row['target_id']}")

    round_ = row["review_round"]
    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (collab["lab_id"],)).fetchone()
    professor = conn.execute(
        "SELECT * FROM professors WHERE id = ?", (lab["professor_id"],)
    ).fetchone()

    rows = conn.execute(
        "SELECT * FROM collaboration_contributions WHERE collaboration_id = ? AND round = ? "
        "ORDER BY student_id",
        (collab["id"], round_),
    ).fetchall()
    if not rows:
        return jobs.fail_job(conn, job_id, lease_id, f"no contributions for round {round_}")

    parts = []
    for contribution in rows:
        path = lab_dir / contribution["path"]
        body = path.read_text() if path.exists() else "(contribution missing)"
        parts.append(f"--- Student {contribution['student_id']} ---\n{body}")

    result = jobs.run_with_session(
        conn,
        job_id,
        backend,
        SYNTHESIS_PROMPT_TEMPLATE.format(
            name=professor["name"],
            field=professor["field"],
            root_problem=lab["root_problem"],
            goal=collab["goal"],
            round=round_,
            shared=render_shared(conn, collab["id"], lab_dir),
            contributions="\n\n".join(parts),
        ),
    )

    if result.rate_limited:
        jobs.record_rate_limit(
            conn, job_id, lease_id, result.retry_after_seconds, provider=backend.name
        )
        return "rate_limited"
    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)

    try:
        payload = extract_json_object(result.text)
        verdict = str(payload["verdict"]).strip().lower()
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        return jobs.fail_job(
            conn, job_id, lease_id, f"unusable synthesis output: {e} -- raw: {result.text[:300]}"
        )
    if verdict not in VALID_VERDICTS:
        return jobs.fail_job(
            conn, job_id, lease_id, f"synthesis verdict {verdict!r} not one of {VALID_VERDICTS}"
        )

    merged = str(payload.get("shared_memory", "")).strip()
    guidance = str(payload.get("guidance", "")).strip()
    if not merged:
        return jobs.fail_job(
            conn, job_id, lease_id, "synthesis returned empty shared_memory; refusing to erase it"
        )

    memory_file = lab_dir / collab["memory_path"]
    checkpoint_artifact(memory_file)
    write_artifact(
        memory_file,
        f"# Collaboration {collab['id']} -- shared state after round {round_}\n\n"
        f"{merged}\n\n## Guidance for next round\n\n{guidance or '(none)'}\n",
    )

    max_rounds = config.max_collaboration_rounds()
    if verdict == "continue" and round_ >= max_rounds:
        # Terminate a non-converging collaboration by writing up what
        # exists rather than discarding it -- same reasoning as the
        # supervision round cap.
        verdict = "ready"

    if verdict == "continue":
        _enqueue_round(conn, collab["id"])
    elif verdict == "ready":
        conn.execute("UPDATE collaborations SET status='writing' WHERE id=?", (collab["id"],))
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('collaboration_write_paper', 'task', ?, 'pending')",
            (collab["task_id"],),
        )
    else:
        conn.execute("UPDATE collaborations SET status='abandoned' WHERE id=?", (collab["id"],))

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"

    record_job_event(
        conn,
        job_id=job_id,
        actor_type="professor",
        actor_id=professor["id"],
        event_type=f"collaboration_{verdict}",
        target_type="task",
        target_id=collab["task_id"],
        payload_path=collab["memory_path"],
    )
    conn.commit()
    return "done"


def record_authors(conn: sqlite3.Connection, paper_id: int, collab_id: int) -> list[int]:
    """Write the byline: lead first, then co-authors in join order."""
    members = _members(conn, collab_id)
    ordered = []
    for position, member in enumerate(members, start=1):
        conn.execute(
            "INSERT OR REPLACE INTO paper_authors (paper_id, student_id, author_order) "
            "VALUES (?, ?, ?)",
            (paper_id, member["student_id"], position),
        )
        ordered.append(member["student_id"])
    return ordered


def authors_for(conn: sqlite3.Connection, paper_id: int) -> list[int]:
    return [
        r["student_id"]
        for r in conn.execute(
            "SELECT student_id FROM paper_authors WHERE paper_id = ? ORDER BY author_order",
            (paper_id,),
        )
    ]


SCAN_PROMPT_TEMPLATE = """You are {name}, a professor. Several papers from your lab have been \
accepted. Decide whether any of them should have been ONE paper.

Your lab's root problem:
<root_problem>
{root_problem}
</root_problem>

Accepted papers in your lab:
<papers>
{papers}
</papers>

{existing}

Combining is worth doing only when the papers share a subject deeply enough that together they \
would establish something none of them establishes alone -- a unifying theorem, a resolved \
conflict between their formulations, a result none could reach separately. Papers that merely \
sit in the same area should stay separate.

Be conservative. A collaboration costs several rounds of every author's time, and combining work \
that does not belong together produces a worse paper than either was.

Respond with ONLY a JSON object, no fences, no commentary:
{{"combine": false, "paper_ids": [], "goal": "...", "rationale": "..."}}
Set "combine" to true only if you are confident. "paper_ids" must name at least two accepted \
papers from the list, and "goal" states what combining them is meant to achieve.
"""


def request_scan(conn: sqlite3.Connection, lab_id: int) -> int | None:
    """Queue a scan for combinable work, unless one is already waiting."""
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE kind='collaboration_scan' AND target_id=? "
        "AND status IN ('pending', 'running')",
        (lab_id,),
    ).fetchone()["n"]
    if pending:
        return None
    cur = conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status) "
        "VALUES ('collaboration_scan', 'lab', ?, 'pending')",
        (lab_id,),
    )
    conn.commit()
    return cur.lastrowid


def execute_collaboration_scan_job(
    conn: sqlite3.Connection, job_id: int, backend: Backend, lab_dir: Path
) -> str:
    """Ask the professor whether any accepted papers belong together.

    Until now a collaboration could only be formed by hand: nothing
    noticed that two accepted papers held competing lower bounds that
    could not both be tight, and as separate papers both stood
    indefinitely. This is the noticing.
    """
    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=1800):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (row["target_id"],)).fetchone()
    if lab is None:
        return jobs.fail_job(conn, job_id, lease_id, f"no lab with id={row['target_id']}")
    professor = conn.execute(
        "SELECT * FROM professors WHERE id = ?", (lab["professor_id"],)
    ).fetchone()

    papers = conn.execute(
        "SELECT papers.* FROM papers JOIN tasks ON tasks.id = papers.task_id "
        "WHERE tasks.lab_id = ? AND papers.status = 'accepted' ORDER BY papers.id",
        (lab["id"],),
    ).fetchall()
    if len(papers) < 2:
        jobs.complete_job(conn, job_id, lease_id)
        return "done"

    rendered = []
    for paper in papers:
        path = lab_dir / paper["path"]
        body = path.read_text(errors="replace") if path.exists() else ""
        rendered.append(
            f"--- Paper {paper['id']} (student {paper['student_id']}): {paper['title']} ---\n"
            + body[:7000]
        )

    prior = conn.execute(
        "SELECT id, goal, status FROM collaborations WHERE lab_id = ?", (lab["id"],)
    ).fetchall()
    existing = (
        "Collaborations already formed in this lab (do not propose the same combination again):\n"
        + "\n".join(f"- #{c['id']} [{c['status']}]: {c['goal'][:120]}" for c in prior)
        if prior
        else "No collaborations have been formed in this lab yet."
    )

    result = jobs.run_with_session(
        conn,
        job_id,
        backend,
        SCAN_PROMPT_TEMPLATE.format(
            name=professor["name"],
            root_problem=lab["root_problem"],
            papers="\n\n".join(rendered),
            existing=existing,
        ),
    )

    if result.rate_limited:
        jobs.record_rate_limit(
            conn, job_id, lease_id, result.retry_after_seconds, provider=backend.name
        )
        return "rate_limited"
    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)

    try:
        payload = extract_json_object(result.text)
    except json.JSONDecodeError as e:
        return jobs.fail_job(
            conn, job_id, lease_id, f"unusable scan output: {e} -- raw: {result.text[:300]}"
        )

    if bool(payload.get("combine")):
        ids = [int(x) for x in (payload.get("paper_ids") or []) if str(x).isdigit()]
        chosen = [p for p in papers if p["id"] in ids]
        students = list(dict.fromkeys(p["student_id"] for p in chosen))
        if len(students) >= 2:
            _form_from_scan(
                conn, lab, students, str(payload.get("goal") or "Combine these results."), lab_dir
            )

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"
    record_job_event(
        conn, job_id=job_id, actor_type="professor", actor_id=professor["id"],
        event_type="collaboration_scanned", target_type="lab", target_id=lab["id"],
    )
    conn.commit()
    return "done"


def _form_from_scan(conn, lab, student_ids, goal: str, lab_dir: Path) -> int | None:
    """Create the anchor task a collaboration needs, then form it.

    A collaboration must be anchored to a task whose assigned student is
    the lead author, so a scan-initiated one creates that task and moves
    the lead onto it -- their previous task is finished, which is why
    their paper was accepted in the first place.
    """
    from . import decompose

    lead = student_ids[0]
    task = decompose._normalize_tasks({"tasks": [{
        "title": f"Combined result: {goal[:80]}",
        "direction": "prove",
        "end_criteria": (
            "Resolved when the constituent accepted results appear as one theory with a single "
            "unifying theorem that none established alone, in consistent notation, with any "
            "redundant proof removed and any conflict between their formulations settled."
        ),
        "brief": goal,
    }]})[0]
    task_id = decompose._create_task(conn, lab["id"], task, lab_dir)
    conn.execute(
        "UPDATE students SET task_id = ?, status = 'working' WHERE id = ?", (task_id, lead)
    )
    try:
        return form_collaboration(conn, task_id, student_ids, goal, lab_dir)
    except CollaborationError:
        return None
