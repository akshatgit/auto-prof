"""Tests for professor_decompose -> real task rows (autoprof/decompose.py)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import decompose  # noqa: E402
from autoprof.backends.base import Backend, BackendResult  # noqa: E402
from tests.helpers import fresh_db  # noqa: E402


class ScriptedBackend(Backend):
    name = "scripted"

    def __init__(self, result: BackendResult):
        self.result = result
        self.calls = []

    def run(self, prompt, **opts):
        self.calls.append(prompt)
        return self.result


def _seed_professor(conn) -> dict:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO professors (lab_id, name, field, status, memory_path) "
        "VALUES (NULL, 'Prof Test', 'Complexity Theory', 'active', 'pending')"
    )
    professor_id = cur.lastrowid
    cur.execute(
        "INSERT INTO labs (professor_id, root_problem, status) VALUES (?, 'root problem', 'active')",
        (professor_id,),
    )
    lab_id = cur.lastrowid
    memory_path = f"{lab_id}/professors/{professor_id}/memory.md"
    cur.execute(
        "UPDATE professors SET lab_id = ?, memory_path = ? WHERE id = ?",
        (lab_id, memory_path, professor_id),
    )
    conn.commit()
    return {"professor_id": professor_id, "lab_id": lab_id, "memory_path": memory_path}


def _enqueue(conn, professor_id: int) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status) "
        "VALUES ('professor_decompose', 'professor', ?, 'pending')",
        (professor_id,),
    )
    conn.commit()
    return cur.lastrowid


def _payload(n=2) -> str:
    return json.dumps(
        {
            "strategy": "split by proof technique",
            "tasks": [
                {
                    "title": f"Task {i}",
                    "direction": "prove",
                    "end_criteria": f"resolved when {i} is settled",
                    "brief": f"brief for {i}",
                }
                for i in range(1, n + 1)
            ],
        }
    )


class NormalizeTasksTests(unittest.TestCase):
    def test_rejects_empty_task_list(self):
        with self.assertRaises(decompose.DecomposeError):
            decompose._normalize_tasks({"tasks": []})

    def test_rejects_invalid_direction(self):
        with self.assertRaises(decompose.DecomposeError) as ctx:
            decompose._normalize_tasks(
                {"tasks": [{"title": "t", "direction": "maybe", "end_criteria": "e"}]}
            )
        self.assertIn("maybe", str(ctx.exception))

    def test_rejects_missing_end_criteria(self):
        with self.assertRaises(decompose.DecomposeError):
            decompose._normalize_tasks({"tasks": [{"title": "t", "direction": "prove"}]})

    def test_caps_task_count(self):
        payload = {
            "tasks": [
                {"title": f"t{i}", "direction": "open", "end_criteria": "e"} for i in range(50)
            ]
        }
        self.assertEqual(
            len(decompose._normalize_tasks(payload)), decompose.MAX_TASKS_PER_DECOMPOSITION
        )

    def test_normalizes_direction_case(self):
        tasks = decompose._normalize_tasks(
            {"tasks": [{"title": "t", "direction": "  PROVE ", "end_criteria": "e"}]}
        )
        self.assertEqual(tasks[0]["direction"], "prove")


class ExecuteDecomposeJobTests(unittest.TestCase):
    def test_creates_tasks_students_and_student_work_jobs(self):
        conn = fresh_db()
        ids = _seed_professor(conn)
        job_id = _enqueue(conn, ids["professor_id"])
        backend = ScriptedBackend(BackendResult(text=_payload(2), model_version="m1"))

        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            outcome = decompose.execute_professor_decompose_job(conn, job_id, backend, lab_dir)
            self.assertEqual(outcome, "done")

            tasks = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
            self.assertEqual(len(tasks), 2)
            for task in tasks:
                self.assertEqual(task["status"], "in_progress")
                self.assertIsNotNone(task["assigned_student_id"])
                self.assertTrue((lab_dir / task["brief_path"]).exists())

            students = conn.execute("SELECT * FROM students").fetchall()
            self.assertEqual(len(students), 2)
            for student in students:
                self.assertEqual(student["status"], "working")
                self.assertTrue((lab_dir / student["memory_path"]).exists())

            work_jobs = conn.execute(
                "SELECT * FROM jobs WHERE kind='student_work'"
            ).fetchall()
            self.assertEqual(len(work_jobs), 2)
            self.assertEqual({j["target_id"] for j in work_jobs}, {t["id"] for t in tasks})

        conn.close()

    def test_writes_strategy_into_professor_memory(self):
        conn = fresh_db()
        ids = _seed_professor(conn)
        job_id = _enqueue(conn, ids["professor_id"])
        backend = ScriptedBackend(BackendResult(text=_payload(1)))

        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            decompose.execute_professor_decompose_job(conn, job_id, backend, lab_dir)
            memory = (lab_dir / ids["memory_path"]).read_text()
            self.assertIn("split by proof technique", memory)
            self.assertIn("Task 1", memory)
        conn.close()

    def test_unparseable_output_fails_the_job_without_creating_tasks(self):
        conn = fresh_db()
        ids = _seed_professor(conn)
        job_id = _enqueue(conn, ids["professor_id"])
        backend = ScriptedBackend(BackendResult(text="I could not decompose this."))

        with tempfile.TemporaryDirectory() as d:
            outcome = decompose.execute_professor_decompose_job(
                conn, job_id, backend, Path(d)
            )
        self.assertIn(outcome, ("retrying", "failed"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        conn.close()

    def test_is_idempotent_when_lab_already_has_tasks(self):
        conn = fresh_db()
        ids = _seed_professor(conn)
        backend = ScriptedBackend(BackendResult(text=_payload(2)))

        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            decompose.execute_professor_decompose_job(
                conn, _enqueue(conn, ids["professor_id"]), backend, lab_dir
            )
            # A second decomposition job for the same professor must not
            # double the lab's task list.
            decompose.execute_professor_decompose_job(
                conn, _enqueue(conn, ids["professor_id"]), backend, lab_dir
            )

        self.assertEqual(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 2)
        self.assertEqual(len(backend.calls), 1)
        conn.close()

    def test_rate_limit_leaves_job_pending(self):
        conn = fresh_db()
        ids = _seed_professor(conn)
        job_id = _enqueue(conn, ids["professor_id"])
        backend = ScriptedBackend(BackendResult(text="", rate_limited=True, retry_after_seconds=30))

        with tempfile.TemporaryDirectory() as d:
            outcome = decompose.execute_professor_decompose_job(
                conn, job_id, backend, Path(d)
            )
        self.assertEqual(outcome, "rate_limited")
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
