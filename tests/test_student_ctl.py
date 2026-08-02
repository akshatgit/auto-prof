import tempfile
import unittest
from pathlib import Path

from autoprof.student_ctl import (
    StudentControlError,
    StudentNotFoundError,
    edit_student,
    get_student,
    list_students,
    replay_job,
    resume_student,
    stop_student,
)
from tests.helpers import fresh_db, seed_lab_with_student


def _insert_job(conn, task_id, status="done", kind="student_work"):
    cur = conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status) VALUES (?, 'task', ?, ?)",
        (kind, task_id, status),
    )
    conn.commit()
    return cur.lastrowid


class ListGetStudentTests(unittest.TestCase):
    def test_list_returns_all_students(self):
        conn = fresh_db()
        seed_lab_with_student(conn)
        rows = list_students(conn)
        self.assertEqual(len(rows), 1)
        conn.close()

    def test_get_returns_row(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        row = get_student(conn, ids["student_id"])
        self.assertEqual(row["id"], ids["student_id"])
        conn.close()

    def test_get_missing_raises(self):
        conn = fresh_db()
        with self.assertRaises(StudentNotFoundError):
            get_student(conn, 999)
        conn.close()


class StopResumeTests(unittest.TestCase):
    def test_stop_sets_paused_at_and_returns_true(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        changed = stop_student(conn, ids["student_id"])
        self.assertTrue(changed)
        row = get_student(conn, ids["student_id"])
        self.assertIsNotNone(row["paused_at"])
        conn.close()

    def test_status_unchanged_by_stop(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        stop_student(conn, ids["student_id"])
        row = get_student(conn, ids["student_id"])
        self.assertEqual(row["status"], "working")
        conn.close()

    def test_stop_records_event(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        stop_student(conn, ids["student_id"])
        row = conn.execute("SELECT * FROM events WHERE event_type='student_stopped'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["actor_type"], "human")
        self.assertEqual(row["target_id"], ids["student_id"])
        conn.close()

    def test_stop_is_idempotent_no_duplicate_event(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        first = stop_student(conn, ids["student_id"])
        second = stop_student(conn, ids["student_id"])
        self.assertTrue(first)
        self.assertFalse(second)
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE event_type='student_stopped'"
        ).fetchone()["n"]
        self.assertEqual(count, 1)
        conn.close()

    def test_resume_clears_paused_at(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        stop_student(conn, ids["student_id"])
        changed = resume_student(conn, ids["student_id"])
        self.assertTrue(changed)
        row = get_student(conn, ids["student_id"])
        self.assertIsNone(row["paused_at"])
        conn.close()

    def test_resume_on_non_paused_student_is_noop(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        changed = resume_student(conn, ids["student_id"])
        self.assertFalse(changed)
        conn.close()

    def test_stop_missing_student_raises(self):
        conn = fresh_db()
        with self.assertRaises(StudentNotFoundError):
            stop_student(conn, 999)
        conn.close()


class EditStudentTests(unittest.TestCase):
    def test_edit_status_only(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        edit_student(conn, ids["student_id"], status="stuck")
        row = get_student(conn, ids["student_id"])
        self.assertEqual(row["status"], "stuck")
        conn.close()

    def test_edit_invalid_status_raises_clear_error(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with self.assertRaises(StudentControlError):
            edit_student(conn, ids["student_id"], status="not_a_real_status")
        conn.close()

    def test_edit_requires_at_least_one_field(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with self.assertRaises(StudentControlError):
            edit_student(conn, ids["student_id"])
        conn.close()

    def test_edit_memory_writes_file(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            memory_path = lab_dir / "mem.md"
            memory_path.parent.mkdir(parents=True, exist_ok=True)
            memory_path.write_text("old content")
            conn.execute(
                "UPDATE students SET memory_path=? WHERE id=?",
                ("mem.md", ids["student_id"]),
            )
            edit_student(conn, ids["student_id"], memory_text="new content", lab_dir=lab_dir)
            self.assertEqual(memory_path.read_text(), "new content")
        conn.close()

    def test_edit_records_event(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        edit_student(conn, ids["student_id"], status="stuck")
        row = conn.execute("SELECT * FROM events WHERE event_type='student_edited'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["actor_type"], "human")
        conn.close()

    def test_edit_missing_student_raises(self):
        conn = fresh_db()
        with self.assertRaises(StudentNotFoundError):
            edit_student(conn, 999, status="stuck")
        conn.close()


class ReplayJobTests(unittest.TestCase):
    def test_replay_creates_new_pending_job_same_kind_and_target(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        original_id = _insert_job(conn, ids["task_id"], status="done", kind="student_work")

        new_id = replay_job(conn, original_id)

        new_row = conn.execute("SELECT * FROM jobs WHERE id=?", (new_id,)).fetchone()
        self.assertEqual(new_row["status"], "pending")
        self.assertEqual(new_row["kind"], "student_work")
        self.assertEqual(new_row["target_type"], "task")
        self.assertEqual(new_row["target_id"], ids["task_id"])
        self.assertEqual(new_row["replayed_from_job_id"], original_id)
        conn.close()

    def test_replay_failed_job_allowed(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        original_id = _insert_job(conn, ids["task_id"], status="failed")
        new_id = replay_job(conn, original_id)
        self.assertIsNotNone(new_id)
        conn.close()

    def test_replay_pending_job_rejected(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        original_id = _insert_job(conn, ids["task_id"], status="pending")
        with self.assertRaises(StudentControlError):
            replay_job(conn, original_id)
        conn.close()

    def test_replay_running_job_rejected(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        original_id = _insert_job(conn, ids["task_id"], status="running")
        with self.assertRaises(StudentControlError):
            replay_job(conn, original_id)
        conn.close()

    def test_replay_missing_job_raises(self):
        conn = fresh_db()
        seed_lab_with_student(conn)
        with self.assertRaises(StudentControlError):
            replay_job(conn, 999)
        conn.close()

    def test_replay_records_event_pointing_at_original_job(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        original_id = _insert_job(conn, ids["task_id"], status="done")
        new_id = replay_job(conn, original_id)
        row = conn.execute("SELECT * FROM events WHERE event_type='job_replayed'").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["target_type"], "job")
        self.assertEqual(row["target_id"], original_id)
        self.assertEqual(row["job_id"], new_id)
        conn.close()


if __name__ == "__main__":
    unittest.main()
