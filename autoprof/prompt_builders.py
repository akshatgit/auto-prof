"""Default prompt builders for the job kinds the daemon can dispatch.

Scope note: these build a real prompt from real DB/filesystem state and
write the model's raw output back to memory.md -- they do NOT yet parse
that output into new task/paper rows (that's "advance_state_machines",
docs/TASKS.md Phase 4, not yet built). So running the daemon today
generates real content and updates memory, but a professor_decompose job
won't automatically create task rows -- that wiring is still ahead.
"""

import sqlite3

from .runner import PromptSpec


class PromptBuildError(RuntimeError):
    pass


def build_professor_decompose_prompt(conn: sqlite3.Connection, job_row) -> PromptSpec:
    professor = conn.execute(
        "SELECT * FROM professors WHERE id = ?", (job_row["target_id"],)
    ).fetchone()
    if professor is None:
        raise PromptBuildError(f"no professor with id={job_row['target_id']}")

    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (professor["lab_id"],)).fetchone()
    if lab is None:
        raise PromptBuildError(f"professor {professor['id']} has no lab")

    prompt = (
        f"You are {professor['name']}, a professor in {professor['field']}.\n\n"
        f"Your lab's root problem (your \"soul\"):\n{lab['root_problem']}\n\n"
        "Decompose this into an initial set of concrete research tasks. For each task, "
        "state: a short title, a direction (prove / disprove / open), and end criteria -- "
        "what would make that task resolved. Also record your current strategy for the lab "
        "as a whole. Write your full response as the lab's ongoing working memory."
    )

    return PromptSpec(
        prompt=prompt,
        artifact_relpath=professor["memory_path"],
        event_type="task_decomposed",
        actor_type="professor",
        actor_id=professor["id"],
    )


def build_student_work_prompt(conn: sqlite3.Connection, job_row) -> PromptSpec:
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (job_row["target_id"],)).fetchone()
    if task is None:
        raise PromptBuildError(f"no task with id={job_row['target_id']}")
    if task["assigned_student_id"] is None:
        raise PromptBuildError(f"task {task['id']} has no assigned student")

    student = conn.execute(
        "SELECT * FROM students WHERE id = ?", (task["assigned_student_id"],)
    ).fetchone()
    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (task["lab_id"],)).fetchone()

    prompt = (
        f"You are a PhD student working on the task \"{task['title']}\" "
        f"(direction: {task['direction']}) within the lab whose root problem is:\n"
        f"{lab['root_problem']}\n\n"
        f"End criteria for this task: {task['end_criteria']}\n\n"
        "Work the problem. Report your progress, any candidate results, dead ends already "
        "tried, and your current strategy, as your ongoing working memory."
    )

    return PromptSpec(
        prompt=prompt,
        artifact_relpath=student["memory_path"],
        event_type="student_worked",
        actor_type="student",
        actor_id=student["id"],
    )


def default_builders() -> dict:
    return {
        "professor_decompose": build_professor_decompose_prompt,
        "student_work": build_student_work_prompt,
    }
