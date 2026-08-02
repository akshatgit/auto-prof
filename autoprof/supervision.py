"""The student<->professor supervision loop that runs BEFORE a paper exists.

docs/DESIGN.md §3.2 steps 1-2. The original build had the student work
once and immediately write up, so the professor first saw the research as
a finished paper and the only corrective channel was peer review -- three
independent reviewers, after the fact, at the cost of a full review round.
That is the wrong place to catch "you haven't actually proved this yet".

The loop here is:

    student_work ──► professor_supervision ──► student_work ──► ...
                            │
                            ├─ 'ready'   ──► student_write_paper
                            └─ 'abandon' ──► task abandoned, student freed

It is deliberately long-horizon: the stopping condition is the professor
agreeing the work is ready, not a small fixed number of passes. The round
cap in config.max_supervision_rounds exists only so a loop that never
converges terminates at all -- reaching it forces a write-up rather than
discarding the work.
"""

import json
import sqlite3
import uuid
from pathlib import Path

from . import config, jobs
from .artifacts import write_artifact
from .backends.base import Backend
from .events import record_job_event
from .jsonio import extract_json_object

VALID_VERDICTS = ("continue", "ready", "abandon")

SUPERVISION_PROMPT_TEMPLATE = """You are {name}, a professor in {field}, supervising a PhD \
student on one task in your lab.

Your lab's root problem:
<root_problem>
{root_problem}
</root_problem>

The task you assigned:
<task_brief>
{brief}
</task_brief>

Task: "{title}" (direction: {direction})
End criteria: {end_criteria}

This is supervision meeting number {round}.

{history}

Your student's current working memory -- everything they have established so far:
<student_memory>
{memory}
</student_memory>

Read their work critically, as the person responsible for it. You are NOT a peer reviewer \
writing a verdict on a finished paper; you are the supervisor deciding what happens next. \
Independent reviewers will later check every step and reject the paper if any step fails, so \
it is your job to find the problems now, while they are cheap to fix.

Assess honestly:
- Is there an actual result here yet, or only a plan, a partial argument, or a restatement of \
the problem?
- Is every step actually derived? Point at any step that is asserted rather than proved, or \
that quietly assumes what it is trying to show.
- Are the claims proportionate to what was proved? Overclaiming is the most common way these \
papers fail review.
- Is anything missing that a reviewer will certainly ask for: edge cases (including degenerate \
ones like rank/size 0 or 1), stated assumptions, honest positioning against prior work?
- Is the result significant enough to be worth writing up, or should the student push further \
first? A narrow-but-correct result that reviewers call "elementary" is a real failure mode.

Then decide one of:
- "continue": the student should keep working. You MUST give specific, actionable guidance -- \
name the exact gap, the exact step to fix, or the exact extension to attempt. Vague \
encouragement is useless and wastes a round.
- "ready": the work is genuinely ready to be written up as a paper that could survive \
independent review. Do not say this merely because progress has been made.
- "abandon": this line of attack is not going to work, and the honest move is to stop. Say why.

Respond with ONLY a JSON object, no markdown fences, no commentary before or after, in exactly \
this shape:
{{"verdict": "continue|ready|abandon", "assessment": "...", "guidance": "..."}}
where "assessment" is your honest read of where the work stands, and "guidance" is what the \
student should do next (for "ready", what they must be careful to include when writing up; for \
"abandon", why stopping is right).
"""


class SupervisionError(RuntimeError):
    pass


def render_history(conn: sqlite3.Connection, task_id: int, lab_dir: Path) -> str:
    """Prior meetings, oldest first, so the professor can see whether their
    own guidance was actually followed.

    Without this each meeting would be memoryless and the professor could
    ask for the same fix indefinitely -- the exact failure the loop exists
    to avoid.
    """
    rows = conn.execute(
        "SELECT * FROM supervisions WHERE task_id = ? ORDER BY round", (task_id,)
    ).fetchall()
    if not rows:
        return "This is the first meeting; there is no prior guidance."

    parts = []
    for row in rows:
        path = lab_dir / row["guidance_path"]
        body = path.read_text() if path.exists() else "(guidance file missing)"
        parts.append(f"--- Meeting {row['round']} (you said: {row['verdict']}) ---\n{body}")
    return (
        "Your own guidance from previous meetings on this task, oldest first. "
        "Check whether the student actually acted on it:\n<supervision_history>\n"
        + "\n\n".join(parts)
        + "\n</supervision_history>"
    )


def _next_round(conn: sqlite3.Connection, task_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(round), 0) AS r FROM supervisions WHERE task_id = ?", (task_id,)
    ).fetchone()
    return row["r"] + 1


def execute_professor_supervision_job(
    conn: sqlite3.Connection, job_id: int, backend: Backend, lab_dir: Path
) -> str:
    """Daemon special_handlers signature: (conn, job_id, backend, lab_dir)."""
    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=3600):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["target_id"],)).fetchone()
    if task is None:
        return jobs.fail_job(conn, job_id, lease_id, f"no task with id={row['target_id']}")
    if task["assigned_student_id"] is None:
        return jobs.fail_job(conn, job_id, lease_id, f"task {task['id']} has no assigned student")

    student = conn.execute(
        "SELECT * FROM students WHERE id = ?", (task["assigned_student_id"],)
    ).fetchone()
    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (task["lab_id"],)).fetchone()
    professor = conn.execute(
        "SELECT * FROM professors WHERE id = ?", (student["professor_id"],)
    ).fetchone()

    memory_file = lab_dir / student["memory_path"]
    memory = memory_file.read_text() if memory_file.exists() else "(no memory recorded yet)"
    brief_file = lab_dir / task["brief_path"]
    brief = brief_file.read_text() if brief_file.exists() else "(no brief written)"

    round_ = _next_round(conn, task["id"])

    result = jobs.run_with_session(
        conn,
        job_id,
        backend,
        SUPERVISION_PROMPT_TEMPLATE.format(
            name=professor["name"],
            field=professor["field"],
            root_problem=lab["root_problem"],
            brief=brief,
            title=task["title"],
            direction=task["direction"],
            end_criteria=task["end_criteria"],
            round=round_,
            history=render_history(conn, task["id"], lab_dir),
            memory=memory,
        ),
    )

    if result.rate_limited:
        jobs.record_rate_limit(conn, job_id, lease_id, result.retry_after_seconds)
        return "rate_limited"
    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)

    try:
        payload = extract_json_object(result.text)
        verdict = str(payload["verdict"]).strip().lower()
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        return jobs.fail_job(
            conn, job_id, lease_id, f"unusable supervision output: {e} -- raw: {result.text[:300]}"
        )
    if verdict not in VALID_VERDICTS:
        return jobs.fail_job(
            conn, job_id, lease_id, f"supervision verdict {verdict!r} not one of {VALID_VERDICTS}"
        )

    max_rounds = config.max_supervision_rounds()
    forced = False
    if verdict == "continue" and round_ >= max_rounds:
        # Terminate a loop that isn't converging -- but by writing up what
        # exists, not by discarding it. The research is real work; only the
        # supervisor's appetite for more rounds has run out.
        verdict = "ready"
        forced = True

    relpath = f"{lab['id']}/tasks/{task['id']}/supervision/{round_}.md"
    assessment = str(payload.get("assessment", "")).strip()
    guidance = str(payload.get("guidance", "")).strip()
    write_artifact(
        lab_dir / relpath,
        f"# Supervision meeting {round_} -- verdict: {verdict}"
        + (f" (forced at round cap {max_rounds})" if forced else "")
        + f"\n\n## Assessment\n\n{assessment or '(none given)'}"
        f"\n\n## Guidance\n\n{guidance or '(none given)'}\n",
    )

    conn.execute(
        "INSERT INTO supervisions (task_id, student_id, round, verdict, guidance_path) "
        "VALUES (?, ?, ?, ?, ?)",
        (task["id"], student["id"], round_, verdict, relpath),
    )

    if verdict == "continue":
        conn.execute("UPDATE students SET status = 'working' WHERE id = ?", (student["id"],))
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('student_work', 'task', ?, 'pending')",
            (task["id"],),
        )
    elif verdict == "ready":
        conn.execute("UPDATE students SET status = 'writing_paper' WHERE id = ?", (student["id"],))
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('student_write_paper', 'task', ?, 'pending')",
            (task["id"],),
        )
    else:  # abandon
        conn.execute("UPDATE tasks SET status = 'abandoned' WHERE id = ?", (task["id"],))
        # The schema's trg_tasks_release_student trigger frees the student
        # when a task is abandoned, so don't also write students here.

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"

    record_job_event(
        conn,
        job_id=job_id,
        actor_type="professor",
        actor_id=professor["id"],
        event_type=f"supervision_{verdict}",
        target_type="task",
        target_id=task["id"],
        payload_path=relpath,
    )
    conn.commit()
    return "done"


def render_student_guidance(conn: sqlite3.Connection, task_id: int, lab_dir: Path) -> str:
    """The supervisor's guidance, as the student should see it.

    Distinct from render_history: the professor needs the full record to
    judge whether their advice was followed, while the student needs the
    latest instruction foregrounded and the earlier ones as context. Both
    read the same rows -- this is a presentation difference, and getting it
    wrong (burying the current instruction in a wall of history) is how
    guidance gets ignored.
    """
    rows = conn.execute(
        "SELECT * FROM supervisions WHERE task_id = ? ORDER BY round", (task_id,)
    ).fetchall()
    if not rows:
        return "You have not met with your supervisor yet on this task."

    def body(row):
        path = lab_dir / row["guidance_path"]
        return path.read_text() if path.exists() else "(guidance file missing)"

    latest = rows[-1]
    out = [
        "Your supervisor has read your work. Their most recent guidance is below and you are "
        "expected to act on it:",
        f"<supervisor_guidance round=\"{latest['round']}\">\n{body(latest)}\n</supervisor_guidance>",
    ]
    if len(rows) > 1:
        earlier = "\n\n".join(f"--- Meeting {r['round']} ---\n{body(r)}" for r in rows[:-1])
        out.append(
            "Earlier guidance on this task, for context (do not re-litigate points you have "
            f"already addressed):\n<earlier_guidance>\n{earlier}\n</earlier_guidance>"
        )
    return "\n\n".join(out)
