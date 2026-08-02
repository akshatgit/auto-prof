"""Shared test helpers. No real subprocess/network calls happen here."""

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "docs" / "schema.sql"


def fresh_db() -> sqlite3.Connection:
    """An in-memory DB with docs/schema.sql applied and foreign keys on."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    return conn


def seed_lab_with_student(conn: sqlite3.Connection) -> dict:
    """Minimal professor/lab/task/student fixture, returns their ids.

    Paths are lab_dir-relative (<lab_id>/...), matching what the
    real code stores -- see create_prof.persist_professor -- so tests
    exercising path-building logic see realistic values rather than
    bootstrap placeholders.
    """
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO professors (lab_id, name, field, status, memory_path) "
        "VALUES (NULL, 'Prof Test', 'Testing', 'active', 'pending')"
    )
    professor_id = cur.lastrowid
    cur.execute(
        "INSERT INTO labs (professor_id, root_problem, status) VALUES (?, 'test problem', 'active')",
        (professor_id,),
    )
    lab_id = cur.lastrowid
    professor_memory_path = f"{lab_id}/professors/{professor_id}/memory.md"
    cur.execute(
        "UPDATE professors SET lab_id = ?, memory_path = ? WHERE id = ?",
        (lab_id, professor_memory_path, professor_id),
    )
    cur.execute(
        "INSERT INTO tasks (lab_id, title, brief_path, direction, end_criteria, status) "
        "VALUES (?, 'Task 1', 'brief.md', 'prove', 'done when proved', 'open')",
        (lab_id,),
    )
    task_id = cur.lastrowid
    student_memory_path = f"{lab_id}/students/1/memory.md"
    cur.execute(
        "INSERT INTO students (task_id, professor_id, status, memory_path) "
        "VALUES (?, ?, 'working', ?)",
        (task_id, professor_id, student_memory_path),
    )
    student_id = cur.lastrowid
    conn.commit()
    return {
        "professor_id": professor_id,
        "lab_id": lab_id,
        "task_id": task_id,
        "student_id": student_id,
        "professor_memory_path": professor_memory_path,
        "student_memory_path": student_memory_path,
    }
