import tempfile
import unittest
from pathlib import Path

from autoprof import prompt_builders
from tests.helpers import fresh_db, seed_lab_with_student


class ProfessorDecomposeBuilderTests(unittest.TestCase):
    def test_prompt_includes_root_problem_and_returns_memory_artifact(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_row = {"target_type": "professor", "target_id": ids["professor_id"]}

        spec = prompt_builders.build_professor_decompose_prompt(conn, job_row)

        self.assertIn("test problem", spec.prompt)
        self.assertEqual(spec.artifact_relpath, ids["professor_memory_path"])
        self.assertEqual(spec.actor_type, "professor")
        self.assertEqual(spec.actor_id, ids["professor_id"])
        self.assertEqual(spec.event_type, "task_decomposed")
        conn.close()

    def test_missing_professor_raises(self):
        conn = fresh_db()
        job_row = {"target_type": "professor", "target_id": 999}
        with self.assertRaises(prompt_builders.PromptBuildError):
            prompt_builders.build_professor_decompose_prompt(conn, job_row)
        conn.close()


class StudentWorkBuilderTests(unittest.TestCase):
    def test_prompt_includes_task_brief_and_root_problem(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_row = {"target_type": "task", "target_id": ids["task_id"]}

        spec = prompt_builders.build_student_work_prompt(conn, job_row)

        self.assertIn("test problem", spec.prompt)
        self.assertIn("Task 1", spec.prompt)
        self.assertEqual(spec.artifact_relpath, ids["student_memory_path"])
        self.assertEqual(spec.actor_type, "student")
        self.assertEqual(spec.actor_id, ids["student_id"])
        self.assertEqual(spec.event_type, "student_worked")
        conn.close()

    def test_task_with_no_assigned_student_raises(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        conn.execute("UPDATE tasks SET assigned_student_id = NULL WHERE id = ?", (ids["task_id"],))
        conn.execute("UPDATE students SET task_id = NULL WHERE id = ?", (ids["student_id"],))
        conn.commit()
        job_row = {"target_type": "task", "target_id": ids["task_id"]}

        with self.assertRaises(prompt_builders.PromptBuildError):
            prompt_builders.build_student_work_prompt(conn, job_row)
        conn.close()

    def test_missing_task_raises(self):
        conn = fresh_db()
        job_row = {"target_type": "task", "target_id": 999}
        with self.assertRaises(prompt_builders.PromptBuildError):
            prompt_builders.build_student_work_prompt(conn, job_row)
        conn.close()


class DefaultBuildersRegistryTests(unittest.TestCase):
    def test_default_builders_cover_professor_decompose_and_student_work(self):
        builders = prompt_builders.default_builders()
        self.assertIn("professor_decompose", builders)
        self.assertIn("student_work", builders)


if __name__ == "__main__":
    unittest.main()
