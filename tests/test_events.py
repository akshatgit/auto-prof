import unittest

from autoprof.events import record_human_event, record_job_event
from tests.helpers import fresh_db, seed_lab_with_student


class RecordHumanEventTests(unittest.TestCase):
    def test_inserts_row_with_null_job_id_and_human_actor(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        record_human_event(
            conn,
            event_type="student_stopped",
            target_type="student",
            target_id=ids["student_id"],
        )
        conn.commit()
        row = conn.execute("SELECT * FROM events").fetchone()
        self.assertIsNone(row["job_id"])
        self.assertEqual(row["actor_type"], "human")
        self.assertEqual(row["event_type"], "student_stopped")
        self.assertEqual(row["target_id"], ids["student_id"])
        conn.close()

    def test_payload_path_optional(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        record_human_event(
            conn, event_type="student_edited", target_type="student", target_id=ids["student_id"]
        )
        conn.commit()
        row = conn.execute("SELECT * FROM events").fetchone()
        self.assertIsNone(row["payload_path"])
        conn.close()


class RecordJobEventTests(unittest.TestCase):
    def test_requires_job_id_and_non_human_actor(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) VALUES ('student_work', 'task', ?, 'done')",
            (ids["task_id"],),
        )
        job_id = conn.execute("SELECT id FROM jobs").fetchone()["id"]
        record_job_event(
            conn,
            job_id=job_id,
            actor_type="student",
            actor_id=ids["student_id"],
            event_type="paper_submitted",
            target_type="task",
            target_id=ids["task_id"],
        )
        conn.commit()
        row = conn.execute("SELECT * FROM events").fetchone()
        self.assertEqual(row["job_id"], job_id)
        self.assertEqual(row["actor_type"], "student")
        conn.close()


if __name__ == "__main__":
    unittest.main()
