"""The professor callback -- docs/DESIGN.md §3.3.

The last structural gap. Everything else in the lifecycle ran itself, but
nothing ever decided that a task was DONE: an accepted paper left its task
at `pending_prof_review` forever, students stayed bound to finished work,
and a rejected paper that the revise loop declined to retry was simply a
dead end. Over one long session that decision was made by hand four times,
and the right answer differed every time -- create a synthesis task, keep
revising, go back to research, re-scope -- which is exactly why it cannot
be a hard-coded response to rejection.

The professor sees the task's whole accumulated state (every paper, every
verdict, the reviewers' actual rationales, the supervision history) and
picks one of four:

    resolved   -> task completed; the student is then either nominated or
                  reassigned
    keep_going -> task stays open, optionally with refined end criteria
    split      -> child tasks under the same lab, each with its own student
    abandon    -> task closed unresolved, student released

Nomination is a SEPARATE decision (§3.3): closing one task does not make a
student a candidate: their *cumulative* accepted work has to look like a
dissertation. A student who is not nominated is reassigned rather than
left holding a finished task.
"""

import json
import sqlite3
import uuid
from pathlib import Path

from . import decompose, jobs
from .artifacts import write_artifact
from .backends.base import Backend
from .events import record_job_event
from .jsonio import extract_json_object

VALID_DECISIONS = ("resolved", "keep_going", "split", "abandon")

CALLBACK_PROMPT_TEMPLATE = """You are {name}, a professor in {field}. One of your tasks has \
reached a decision point and you must decide what happens to it.

Your lab's root problem:
<root_problem>
{root_problem}
</root_problem>

The task:
<task>
Title: {title}
Direction: {direction}
Status: {status}
End criteria: {end_criteria}
</task>

{brief}

What the assigned student has produced on it:
<papers>
{papers}
</papers>

{supervision}

{student_record}

Decide what happens to this TASK. This is a judgement about the task's question, not about the \
student.

- "resolved": the task's question is settled -- either affirmatively, or by a negative result \
that genuinely closes it. A paper being accepted does NOT automatically mean this: ask whether \
the end criteria are actually met. Equally, a task can be resolved by a rejected paper whose \
mathematics nonetheless settled the question.
- "keep_going": there is more to get here. You may refine the end criteria if the original \
framing has proven wrong; say what changed and why.
- "split": the task contains several independent questions and should become child tasks. Give \
each child a title, direction, end criteria and brief, exactly as in a decomposition. Say \
whether the parent closes or stays open.
- "abandon": this question is not going to be settled by this line of attack -- it was \
ill-posed, or it has been subsumed, or the approach is exhausted. Say plainly why stopping is \
right. This is a legitimate outcome, not a failure.

Separately, decide whether to NOMINATE this student for a dissertation defense. Judge their \
CUMULATIVE accepted work across every task they have worked, not this one task. Closing a single \
task is normally not sufficient. Be conservative: a nomination that fails a 5-reviewer defense \
costs far more than a delayed one.

Respond with ONLY a JSON object, no markdown fences, no commentary, in exactly this shape:
{{"decision": "resolved|keep_going|split|abandon",
  "rationale": "...",
  "refined_end_criteria": "... or null",
  "parent_closes": true,
  "children": [{{"title": "...", "direction": "prove|disprove|open|implement", \
"end_criteria": "...", "brief": "..."}}],
  "nominate": false,
  "nomination_rationale": "..."}}
"children" is used only for "split"; "refined_end_criteria" only for "keep_going".
"""


class CallbackError(RuntimeError):
    pass


def request_callback(conn: sqlite3.Connection, task_id: int) -> int | None:
    """Enqueue a callback for a task, unless one is already waiting.

    Idempotent because the callback fires from several places (a paper
    accepted, a revise loop declining to continue, a human asking) and two
    concurrent callbacks on one task could split it twice.
    """
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE kind='professor_callback' AND target_id=? "
        "AND status IN ('pending', 'running')",
        (task_id,),
    ).fetchone()["n"]
    if pending:
        return None
    cur = conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status) "
        "VALUES ('professor_callback', 'task', ?, 'pending')",
        (task_id,),
    )
    conn.commit()
    return cur.lastrowid


def _render_papers(conn, task_id: int, lab_dir: Path) -> str:
    rows = conn.execute(
        "SELECT * FROM papers WHERE task_id = ? ORDER BY id", (task_id,)
    ).fetchall()
    if not rows:
        return "No papers have been submitted for this task."

    parts = []
    for paper in rows:
        verdicts = conn.execute(
            "SELECT review_round, reviewer_index, verdict, rationale_path FROM reviews "
            "WHERE target_type='paper' AND target_id=? ORDER BY review_round, reviewer_index",
            (paper["id"],),
        ).fetchall()
        summary = ", ".join(f"r{v['review_round']}:{v['verdict']}" for v in verdicts) or "no reviews"
        parts.append(f"Paper {paper['id']} [{paper['status']}] '{paper['title']}' -- {summary}")

        # The most recent round's rationales: what the reviewers actually
        # said matters far more than the verdict labels when deciding
        # whether the question is settled.
        if verdicts:
            last_round = verdicts[-1]["review_round"]
            for v in verdicts:
                if v["review_round"] != last_round:
                    continue
                path = lab_dir / v["rationale_path"]
                body = path.read_text(errors="replace") if path.exists() else ""
                if body:
                    parts.append(
                        f"  --- reviewer {v['reviewer_index']} ({v['verdict']}), round "
                        f"{last_round} ---\n{body[:2500]}"
                    )
    return "\n\n".join(parts)


def _render_student_record(conn, student_id: int | None) -> str:
    if student_id is None:
        return "This task has no assigned student."
    accepted = conn.execute(
        "SELECT papers.id, papers.title FROM papers WHERE papers.student_id = ? "
        "AND papers.status = 'accepted' ORDER BY papers.id",
        (student_id,),
    ).fetchall()
    coauthored = conn.execute(
        "SELECT papers.id, papers.title FROM papers "
        "JOIN paper_authors ON paper_authors.paper_id = papers.id "
        "WHERE paper_authors.student_id = ? AND papers.status = 'accepted' "
        "AND papers.student_id != ? ORDER BY papers.id",
        (student_id, student_id),
    ).fetchall()

    lines = [f"Student {student_id}'s cumulative record across ALL tasks:"]
    lines.append(f"  Accepted as lead author: {len(accepted)}")
    for row in accepted:
        lines.append(f"    - paper {row['id']}: {row['title'][:90]}")
    lines.append(f"  Accepted as co-author: {len(coauthored)}")
    for row in coauthored:
        lines.append(f"    - paper {row['id']}: {row['title'][:90]}")
    return "\n".join(lines)


def _apply_split(conn, task, payload, lab_dir: Path) -> list[int]:
    children = payload.get("children") or []
    try:
        normalized = decompose._normalize_tasks({"tasks": children})
    except decompose.DecomposeError as e:
        raise CallbackError(f"split produced unusable child tasks: {e}") from e

    created = []
    professor_id = conn.execute(
        "SELECT professor_id FROM labs WHERE id = ?", (task["lab_id"],)
    ).fetchone()["professor_id"]
    for child in normalized:
        child_id = decompose._create_task(conn, task["lab_id"], child, lab_dir)
        conn.execute(
            "UPDATE tasks SET parent_task_id = ? WHERE id = ?", (task["id"], child_id)
        )
        student_id = decompose._assign_student(conn, professor_id, task["lab_id"], child_id, lab_dir)
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('student_work', 'task', ?, 'pending')",
            (child_id,),
        )
        created.append((child_id, student_id))
    return created


def _reassign_or_release(conn, student_id: int, lab_id: int) -> str:
    """Move a student off a closed task (§3.1).

    Writes students.task_id only -- tasks.assigned_student_id is a derived
    back-pointer maintained by the schema's triggers, and writing it
    directly would race them.
    """
    open_task = conn.execute(
        "SELECT id FROM tasks WHERE lab_id = ? AND status = 'open' "
        "AND assigned_student_id IS NULL ORDER BY id LIMIT 1",
        (lab_id,),
    ).fetchone()
    if open_task:
        conn.execute(
            "UPDATE students SET task_id = ?, status = 'working' WHERE id = ?",
            (open_task["id"], student_id),
        )
        conn.execute("UPDATE tasks SET status = 'in_progress' WHERE id = ?", (open_task["id"],))
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('student_work', 'task', ?, 'pending')",
            (open_task["id"],),
        )
        return f"reassigned to task {open_task['id']}"

    conn.execute(
        "UPDATE students SET task_id = NULL, status = 'unassigned' WHERE id = ?", (student_id,)
    )
    return "left unassigned (no open task in the lab)"


def execute_professor_callback_job(
    conn: sqlite3.Connection, job_id: int, backend: Backend, lab_dir: Path
) -> str:
    """Daemon special_handlers signature: (conn, job_id, backend, lab_dir)."""
    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=1800):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["target_id"],)).fetchone()
    if task is None:
        return jobs.fail_job(conn, job_id, lease_id, f"no task with id={row['target_id']}")
    if task["status"] in ("completed", "abandoned"):
        # Already closed by an earlier callback; nothing to decide.
        jobs.complete_job(conn, job_id, lease_id)
        return "done"

    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (task["lab_id"],)).fetchone()
    professor = conn.execute(
        "SELECT * FROM professors WHERE id = ?", (lab["professor_id"],)
    ).fetchone()
    student_id = task["assigned_student_id"]

    brief_file = lab_dir / task["brief_path"]
    brief = (
        f"The brief you wrote for it:\n<task_brief>\n{brief_file.read_text(errors='replace')}\n"
        "</task_brief>"
        if brief_file.exists()
        else ""
    )

    from . import supervision

    result = jobs.run_with_session(
        conn,
        job_id,
        backend,
        CALLBACK_PROMPT_TEMPLATE.format(
            name=professor["name"],
            field=professor["field"],
            root_problem=lab["root_problem"],
            title=task["title"],
            direction=task["direction"],
            status=task["status"],
            end_criteria=task["end_criteria"],
            brief=brief,
            papers=_render_papers(conn, task["id"], lab_dir),
            supervision=supervision.render_history(conn, task["id"], lab_dir),
            student_record=_render_student_record(conn, student_id),
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
        decision = str(payload["decision"]).strip().lower()
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        return jobs.fail_job(
            conn, job_id, lease_id, f"unusable callback output: {e} -- raw: {result.text[:300]}"
        )
    if decision not in VALID_DECISIONS:
        return jobs.fail_job(
            conn, job_id, lease_id, f"callback decision {decision!r} not one of {VALID_DECISIONS}"
        )

    rationale = str(payload.get("rationale", "")).strip() or "(none given)"
    notes = [f"# Professor decision: {decision}", "", rationale]
    closed = False

    if decision == "keep_going":
        refined = payload.get("refined_end_criteria")
        if refined and str(refined).strip().lower() not in ("null", "none", ""):
            conn.execute(
                "UPDATE tasks SET end_criteria = ? WHERE id = ?",
                (str(refined).strip(), task["id"]),
            )
            notes.append(f"\n## Refined end criteria\n\n{str(refined).strip()}")
        conn.execute("UPDATE tasks SET status = 'in_progress' WHERE id = ?", (task["id"],))
        if student_id is not None:
            conn.execute("UPDATE students SET status = 'working' WHERE id = ?", (student_id,))
            conn.execute(
                "INSERT INTO jobs (kind, target_type, target_id, status) "
                "VALUES ('student_work', 'task', ?, 'pending')",
                (task["id"],),
            )

    elif decision == "split":
        try:
            created = _apply_split(conn, task, payload, lab_dir)
        except CallbackError as e:
            return jobs.fail_job(conn, job_id, lease_id, str(e))
        notes.append(
            "\n## Children\n\n"
            + "\n".join(f"- task {tid} (student {sid})" for tid, sid in created)
        )
        if payload.get("parent_closes", True):
            conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (task["id"],))
            closed = True
        else:
            conn.execute("UPDATE tasks SET status = 'in_progress' WHERE id = ?", (task["id"],))

    elif decision == "resolved":
        conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (task["id"],))
        closed = True

    else:  # abandon
        conn.execute("UPDATE tasks SET status = 'abandoned' WHERE id = ?", (task["id"],))
        # trg_tasks_release_student frees the student automatically.
        closed = True

    # Nomination is a separate decision (§3.3) and only arises on a task
    # the professor just closed.
    nominated = False
    if closed and student_id is not None and decision != "abandon":
        if bool(payload.get("nominate")):
            conn.execute(
                "UPDATE students SET status = 'defending' WHERE id = ?", (student_id,)
            )
            nominated = True
            from . import defense

            defense.request_defense(conn, student_id)
            notes.append(
                "\n## Nomination\n\n"
                + (str(payload.get("nomination_rationale", "")).strip() or "(none given)")
            )
        else:
            outcome = _reassign_or_release(conn, student_id, task["lab_id"])
            notes.append(f"\n## Student\n\nNot nominated; {outcome}.")

    relpath = f"{lab['id']}/tasks/{task['id']}/decision.md"
    write_artifact(lab_dir / relpath, "\n".join(notes) + "\n")

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"

    record_job_event(
        conn,
        job_id=job_id,
        actor_type="professor",
        actor_id=professor["id"],
        event_type=f"task_{decision}" + ("_nominated" if nominated else ""),
        target_type="task",
        target_id=task["id"],
        payload_path=relpath,
    )
    conn.commit()
    return "done"
