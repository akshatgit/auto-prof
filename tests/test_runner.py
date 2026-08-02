import tempfile
import unittest
from pathlib import Path

from autoprof.backends.base import Backend, BackendResult
from autoprof.runner import PromptSpec, execute_job
from tests.helpers import fresh_db, seed_lab_with_student


def _insert_pending_job(conn, task_id, kind="student_work", max_attempts=5):
    cur = conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status, max_attempts) "
        "VALUES (?, 'task', ?, 'pending', ?)",
        (kind, task_id, max_attempts),
    )
    conn.commit()
    return cur.lastrowid


class ScriptedBackend(Backend):
    name = "scripted"

    def __init__(self, result: BackendResult):
        self.result = result
        self.calls = []

    def run(self, prompt, **opts):
        self.calls.append(prompt)
        return self.result


class ExecuteJobSuccessTests(unittest.TestCase):
    def test_success_writes_artifact_and_completes_job(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"])
        backend = ScriptedBackend(BackendResult(text="the paper draft", model_version="m1"))

        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)

            def builder(conn, row):
                return PromptSpec(
                    prompt="write the paper",
                    artifact_relpath="tasks/1/papers/1/draft.md",
                    event_type="paper_drafted",
                    actor_type="student",
                    actor_id=ids["student_id"],
                )

            outcome = execute_job(
                conn,
                job_id,
                backend=backend,
                prompt_builders={"student_work": builder},
                lab_dir=lab_dir,
            )

            self.assertEqual(outcome, "done")
            artifact = lab_dir / "tasks/1/papers/1/draft.md"
            self.assertEqual(artifact.read_text(), "the paper draft")

            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            self.assertEqual(row["status"], "done")
            self.assertEqual(row["model_version"], "m1")

            event = conn.execute("SELECT * FROM events WHERE event_type='paper_drafted'").fetchone()
            self.assertIsNotNone(event)
            self.assertEqual(event["job_id"], job_id)
            self.assertEqual(event["actor_type"], "student")
            self.assertEqual(event["payload_path"], "tasks/1/papers/1/draft.md")
        conn.close()

    def test_success_without_artifact_still_completes(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"], kind="professor_callback")
        backend = ScriptedBackend(BackendResult(text="decision: keep going"))

        def builder(conn, row):
            return PromptSpec(
                prompt="decide",
                artifact_relpath=None,
                event_type="callback_decided",
                actor_type="professor",
                actor_id=ids["professor_id"],
            )

        with tempfile.TemporaryDirectory() as d:
            outcome = execute_job(
                conn, job_id, backend=backend,
                prompt_builders={"professor_callback": builder}, lab_dir=Path(d),
            )
        self.assertEqual(outcome, "done")
        conn.close()

    def test_prompt_passed_to_backend(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"])
        backend = ScriptedBackend(BackendResult(text="ok"))

        def builder(conn, row):
            return PromptSpec(
                prompt="a very specific prompt",
                artifact_relpath=None,
                event_type="x",
                actor_type="student",
                actor_id=None,
            )

        with tempfile.TemporaryDirectory() as d:
            execute_job(conn, job_id, backend=backend, prompt_builders={"student_work": builder}, lab_dir=Path(d))
        self.assertEqual(backend.calls, ["a very specific prompt"])
        conn.close()


class ExecuteJobRateLimitTests(unittest.TestCase):
    def test_rate_limited_leaves_job_pending_with_backoff(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"])
        backend = ScriptedBackend(BackendResult(text="", rate_limited=True, retry_after_seconds=30.0))

        def builder(conn, row):
            return PromptSpec(prompt="p", artifact_relpath=None, event_type="x", actor_type="student", actor_id=None)

        with tempfile.TemporaryDirectory() as d:
            outcome = execute_job(conn, job_id, backend=backend, prompt_builders={"student_work": builder}, lab_dir=Path(d))

        self.assertEqual(outcome, "rate_limited")
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["rate_limit_count"], 1)
        conn.close()


class ExecuteJobErrorTests(unittest.TestCase):
    def test_backend_error_retries(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"], max_attempts=5)
        backend = ScriptedBackend(BackendResult(text="", error="backend exploded"))

        def builder(conn, row):
            return PromptSpec(prompt="p", artifact_relpath=None, event_type="x", actor_type="student", actor_id=None)

        with tempfile.TemporaryDirectory() as d:
            outcome = execute_job(conn, job_id, backend=backend, prompt_builders={"student_work": builder}, lab_dir=Path(d))

        self.assertEqual(outcome, "retrying")
        row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["attempts"], 1)
        conn.close()

    def test_missing_prompt_builder_fails_the_job(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"], kind="memory_compact", max_attempts=1)
        backend = ScriptedBackend(BackendResult(text="unused"))

        with tempfile.TemporaryDirectory() as d:
            outcome = execute_job(conn, job_id, backend=backend, prompt_builders={}, lab_dir=Path(d))

        self.assertEqual(outcome, "failed")
        self.assertEqual(backend.calls, [], "backend must never be called without a prompt")
        conn.close()

    def test_builder_exception_fails_the_job_not_raises(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"], max_attempts=1)
        backend = ScriptedBackend(BackendResult(text="unused"))

        def broken_builder(conn, row):
            raise ValueError("cannot build prompt")

        with tempfile.TemporaryDirectory() as d:
            outcome = execute_job(
                conn, job_id, backend=backend,
                prompt_builders={"student_work": broken_builder}, lab_dir=Path(d),
            )

        self.assertEqual(outcome, "failed")
        row = conn.execute("SELECT last_error FROM jobs WHERE id=?", (job_id,)).fetchone()
        self.assertIn("cannot build prompt", row["last_error"])
        conn.close()

    def test_already_running_job_is_not_claimed(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        job_id = _insert_pending_job(conn, ids["task_id"])
        conn.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        conn.commit()
        backend = ScriptedBackend(BackendResult(text="ok"))

        def builder(conn, row):
            return PromptSpec(prompt="p", artifact_relpath=None, event_type="x", actor_type="student", actor_id=None)

        with tempfile.TemporaryDirectory() as d:
            outcome = execute_job(conn, job_id, backend=backend, prompt_builders={"student_work": builder}, lab_dir=Path(d))

        self.assertEqual(outcome, "not_claimed")
        self.assertEqual(backend.calls, [])
        conn.close()


if __name__ == "__main__":
    unittest.main()
