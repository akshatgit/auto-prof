"""professor_decompose: turn a lab's root problem into real `tasks` rows.

Closes the biggest gap called out in docs/TASKS.md "Explicitly deferred":
the old generic prompt-builder path wrote the professor's decomposition
into memory.md as prose and stopped there, so nothing downstream ever
happened. This handler asks for structured JSON instead, then actually
creates the task rows, seeds a student per task, and enqueues the
student_work jobs that carry the lab forward.

Structured output is requested as JSON rather than parsed out of prose on
purpose: a task row has a CHECK-constrained `direction` and a NOT NULL
`end_criteria`, and guessing those out of free text is exactly the kind of
silent-corruption source that a multi-year run can't recover from.
"""

import json
import sqlite3
import uuid
from pathlib import Path

from . import config, ingest, jobs
from .artifacts import checkpoint_artifact, write_artifact
from .backends.base import Backend
from .events import record_job_event
from .jsonio import extract_json_object

# The per-decomposition task cap now lives in autoprof/config.py so a lab
# spanning several independent problems can raise it. Kept as an alias for
# callers and tests that referenced it directly.
MAX_TASKS_PER_DECOMPOSITION = config.DEFAULT_MAX_TASKS_PER_DECOMPOSITION

# The authority on what a direction may be -- tasks.direction carries no
# CHECK constraint, because SQLite cannot alter one without a table
# rebuild and the deployed schema then diverges from docs/schema.sql
# unnoticed.
VALID_DIRECTIONS = ("prove", "disprove", "open", "implement")

DECOMPOSE_PROMPT_TEMPLATE = """You are {name}, a professor in {field}, leading a research lab.

Your lab's root problem (your "soul" -- the enduring question your lab exists to answer):
<root_problem>
{root_problem}
</root_problem>

{corpus}

Decompose this root problem into an initial set of {max_tasks} or fewer concrete research \
tasks that PhD students in your lab will work on. Each task must be small enough that a \
single focused student could plausibly resolve it and produce one paper, and specific \
enough that you could later judge it "resolved" or not.

For each task give:
- "title": a short, specific title.
- "direction": exactly one of "prove", "disprove", "open", or "implement" -- what you are \
asking the student to attempt. Use "open" only for genuinely exploratory tasks where you \
are not asserting which way the answer goes. Use "implement" when the deliverable is a \
working artifact and evidence about its behaviour -- a tool, a mechanism, a change to a \
system -- rather than an argument: an implement task is resolved by the artifact existing, \
doing what was claimed, and being measured, not by a proof. Do not label an engineering \
task "prove"; a student told to prove a monitor will try to, and fail.
- "end_criteria": what would make this task resolved. Be concrete about what would count \
as success, and what would count as a negative result that still closes the task.
- "brief": 2-4 paragraphs the assigned student will read as their full instructions -- \
context, why this task matters to the root problem, what is already known, what approach \
you suggest they try first, and what pitfalls to avoid.

Also give "strategy": your current thinking about the lab as a whole -- why you split the \
problem this way, which task you expect to be hardest, and what you would do if the \
decomposition turns out to be wrong.

Respond with ONLY a JSON object, no markdown code fences, no commentary before or after, \
in exactly this shape:
{{"strategy": "...", "tasks": [{{"title": "...", "direction": "prove", "end_criteria": "...", \
"brief": "..."}}]}}
"""

MEMORY_TEMPLATE = """# Professor: {name}

Field: {field}

## Root Problem (the lab's soul)

{root_problem}

## Strategy

{strategy}

## Open Tasks (as decomposed)

{task_list}
"""


class DecomposeError(RuntimeError):
    pass


def _normalize_tasks(payload: dict, max_tasks: int | None = None) -> list[dict]:
    """Validate the model's task list before any of it reaches the DB.

    Rejects rather than repairs: a task with a direction the schema won't
    accept, or with no end criteria, is a decomposition the professor
    should redo (the job retries), not one to silently patch up.
    """
    if max_tasks is None:
        max_tasks = config.max_tasks_per_decomposition()
    raw_tasks = payload.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise DecomposeError(f"decomposition contained no tasks: {payload!r}")

    tasks = []
    for i, task in enumerate(raw_tasks[:max_tasks]):
        if not isinstance(task, dict):
            raise DecomposeError(f"task {i} is not an object: {task!r}")
        missing = {"title", "direction", "end_criteria"} - task.keys()
        if missing:
            raise DecomposeError(f"task {i} missing required keys {sorted(missing)}: {task!r}")
        direction = str(task["direction"]).strip().lower()
        if direction not in VALID_DIRECTIONS:
            raise DecomposeError(
                f"task {i} has direction {task['direction']!r}, "
                f"expected one of {VALID_DIRECTIONS}"
            )
        tasks.append(
            {
                "title": str(task["title"]).strip(),
                "direction": direction,
                "end_criteria": str(task["end_criteria"]).strip(),
                "brief": str(task.get("brief", "")).strip(),
            }
        )
    return tasks


def _create_task(conn, lab_id: int, task: dict, lab_dir: Path) -> int:
    """Insert one task row and write its brief.

    brief_path is NOT NULL but contains the task's own id, so the row is
    inserted with a placeholder and backfilled -- same two-step shape as
    create_prof.persist_professor's professor/lab bootstrap.
    """
    cur = conn.execute(
        "INSERT INTO tasks (lab_id, title, brief_path, direction, end_criteria, status) "
        "VALUES (?, ?, 'pending', ?, ?, 'open')",
        (lab_id, task["title"], task["direction"], task["end_criteria"]),
    )
    task_id = cur.lastrowid

    brief_relpath = f"{lab_id}/tasks/{task_id}/brief.md"
    conn.execute("UPDATE tasks SET brief_path = ? WHERE id = ?", (brief_relpath, task_id))

    brief_body = task["brief"] or "(no brief supplied by the professor)"
    write_artifact(
        lab_dir / brief_relpath,
        f"# Task {task_id}: {task['title']}\n\n"
        f"**Direction:** {task['direction']}\n\n"
        f"## End Criteria\n\n{task['end_criteria']}\n\n"
        f"## Brief\n\n{brief_body}\n",
    )
    return task_id


def _assign_student(conn, professor_id: int, lab_id: int, task_id: int, lab_dir: Path) -> int:
    """Create a student, assign them to the task, seed their memory.

    Insert order matters: students.task_id is set in the INSERT so the
    schema's trg_students_task_assign_insert trigger backfills
    tasks.assigned_student_id for us -- setting it by hand here would race
    that trigger.
    """
    cur = conn.execute(
        "INSERT INTO students (task_id, professor_id, status, memory_path) "
        "VALUES (?, ?, 'working', 'pending')",
        (task_id, professor_id),
    )
    student_id = cur.lastrowid

    memory_relpath = f"{lab_id}/students/{student_id}/memory.md"
    conn.execute("UPDATE students SET memory_path = ? WHERE id = ?", (memory_relpath, student_id))

    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    write_artifact(
        lab_dir / memory_relpath,
        f"# Student {student_id}\n\n"
        f"Assigned task {task_id}: {task['title']} (direction: {task['direction']})\n\n"
        "## Status\n\nJust assigned. No work done yet.\n\n"
        "## Progress\n\nNothing recorded yet.\n\n"
        "## Dead Ends\n\nNone yet.\n",
    )

    conn.execute("UPDATE tasks SET status = 'in_progress' WHERE id = ?", (task_id,))
    return student_id


def execute_professor_decompose_job(
    conn: sqlite3.Connection, job_id: int, backend: Backend, lab_dir: Path
) -> str:
    """Daemon special_handlers signature: (conn, job_id, backend, lab_dir)."""
    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=1800):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    professor = conn.execute(
        "SELECT * FROM professors WHERE id = ?", (row["target_id"],)
    ).fetchone()
    if professor is None:
        return jobs.fail_job(conn, job_id, lease_id, f"no professor with id={row['target_id']}")

    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (professor["lab_id"],)).fetchone()
    if lab is None:
        return jobs.fail_job(conn, job_id, lease_id, f"professor {professor['id']} has no lab")

    # Guard against a second decomposition piling duplicate tasks onto a
    # lab that already has them -- a retry after a partially-applied run,
    # or a manually re-enqueued job.
    #
    # Abandoned tasks do not count. They are not live work, and treating
    # them as a block leaves a lab whose whole decomposition was discarded
    # permanently unable to produce another one -- with deleting the rows
    # as the only way out, which reuses their ids and silently repoints
    # every job and event that referenced them.
    existing = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks WHERE lab_id = ? AND status != 'abandoned'",
        (lab["id"],),
    ).fetchone()["n"]
    if existing > 0:
        jobs.complete_job(conn, job_id, lease_id)
        return "done"

    result = jobs.run_with_session(
        conn,
        job_id,
        backend,
        DECOMPOSE_PROMPT_TEMPLATE.format(
            name=professor["name"],
            field=professor["field"],
            root_problem=lab["root_problem"],
            max_tasks=config.max_tasks_per_decomposition(),
            corpus=ingest.render_corpus(conn, lab['id'], lab_dir),
        )
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
        tasks = _normalize_tasks(payload)
    except (json.JSONDecodeError, DecomposeError) as e:
        return jobs.fail_job(
            conn, job_id, lease_id, f"unusable decomposition: {e} -- raw: {result.text[:300]}"
        )

    created = []
    for task in tasks:
        task_id = _create_task(conn, lab["id"], task, lab_dir)
        student_id = _assign_student(conn, professor["id"], lab["id"], task_id, lab_dir)
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('student_work', 'task', ?, 'pending')",
            (task_id,),
        )
        created.append((task_id, student_id, task))

    task_list = "\n".join(
        f"- Task {tid} ({t['direction']}): {t['title']} -- student {sid}"
        for tid, sid, t in created
    )
    checkpoint_artifact(lab_dir / professor["memory_path"])
    write_artifact(
        lab_dir / professor["memory_path"],
        MEMORY_TEMPLATE.format(
            name=professor["name"],
            field=professor["field"],
            root_problem=lab["root_problem"],
            strategy=str(payload.get("strategy", "(none recorded)")).strip(),
            task_list=task_list,
        ),
    )

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"

    record_job_event(
        conn,
        job_id=job_id,
        actor_type="professor",
        actor_id=professor["id"],
        event_type="task_decomposed",
        target_type="professor",
        target_id=professor["id"],
        payload_path=professor["memory_path"],
    )
    conn.commit()
    return "done"
