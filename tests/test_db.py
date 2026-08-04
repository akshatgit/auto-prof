"""Tests for schema init and additive migrations (autoprof/db.py)."""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import db  # noqa: E402


class EnsureInitializedTests(unittest.TestCase):
    def test_fresh_db_gets_the_full_schema(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(Path(d) / "a.db")
            db.ensure_initialized(conn)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
            self.assertIn("backend_session_id", cols)
            conn.close()

    def test_existing_db_missing_a_column_is_migrated(self):
        """An e2e DB created before backend_session_id existed must gain
        the column rather than break every job."""
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.db"
            conn = db.connect(path)
            db.ensure_initialized(conn)
            conn.execute("ALTER TABLE jobs DROP COLUMN backend_session_id")
            conn.commit()
            cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
            self.assertNotIn("backend_session_id", cols)

            db.ensure_initialized(conn)

            cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
            self.assertIn("backend_session_id", cols)
            conn.close()

    def test_migration_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(Path(d) / "b.db")
            for _ in range(3):
                db.ensure_initialized(conn)
            cols = [r[1] for r in conn.execute("PRAGMA table_info(jobs)")]
            self.assertEqual(cols.count("backend_session_id"), 1)
            conn.close()


if __name__ == "__main__":
    unittest.main()


class TasksDirectionRebuildTests(unittest.TestCase):
    """The tasks.direction CHECK could not be widened in place: SQLite has
    no ALTER for a constraint, so docs/schema.sql and the deployed table
    diverge silently -- jobs.status still carries that bug, where
    'cancelled' is documented and the live constraint rejects it."""

    def _legacy_db(self):
        conn = db.connect(":memory:")
        db.ensure_initialized(conn)
        # Put the OLD constrained table back, exactly as a pre-'implement'
        # database has it, and refill it.
        conn.execute("PRAGMA foreign_keys=OFF")
        # DROP then CREATE, never RENAME: SQLite rewrites references to a
        # renamed table inside OTHER tables' triggers, so renaming `tasks`
        # here silently repoints trg_students_task_assign_insert at the
        # old name and the fixture breaks something the migration didn't.
        conn.execute("DROP TABLE tasks")
        conn.execute(
            "CREATE TABLE tasks (id INTEGER PRIMARY KEY, lab_id INTEGER NOT NULL "
            "REFERENCES labs(id), parent_task_id INTEGER REFERENCES tasks(id), "
            "title TEXT NOT NULL, brief_path TEXT NOT NULL, direction TEXT NOT NULL "
            "CHECK (direction IN ('prove', 'disprove', 'open')), end_criteria TEXT NOT NULL, "
            "status TEXT NOT NULL CHECK (status IN ('open','in_progress',"
            "'pending_prof_review','completed','abandoned')), assigned_student_id INTEGER "
            "REFERENCES students(id), created_at TEXT NOT NULL DEFAULT (datetime('now')), "
            "updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
        )
        for stmt in db._TASKS_INDEXES_AND_TRIGGERS:
            conn.execute(stmt)
        conn.execute(
            "INSERT INTO professors (lab_id, name, field, status, memory_path) "
            "VALUES (NULL, 'P', 'F', 'active', 'm.md')"
        )
        conn.execute(
            "INSERT INTO labs (professor_id, root_problem, status) VALUES (1, 'rp', 'active')"
        )
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        return conn

    def test_legacy_db_rejects_implement_before_the_rebuild(self):
        conn = self._legacy_db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tasks (lab_id, title, brief_path, direction, end_criteria, status)"
                " VALUES (1, 't', 'b.md', 'implement', 'c', 'open')"
            )
        conn.close()

    def test_rebuild_accepts_implement_and_keeps_ids(self):
        conn = self._legacy_db()
        for d in ("prove", "disprove", "open"):
            conn.execute(
                "INSERT INTO tasks (lab_id, title, brief_path, direction, end_criteria, status)"
                " VALUES (1, ?, 'b.md', ?, 'c', 'open')",
                (d, d),
            )
        conn.commit()
        before = conn.execute("SELECT id, title FROM tasks ORDER BY id").fetchall()

        db.ensure_initialized(conn)

        after = conn.execute("SELECT id, title FROM tasks ORDER BY id").fetchall()
        self.assertEqual([tuple(r) for r in before], [tuple(r) for r in after])
        conn.execute(
            "INSERT INTO tasks (lab_id, title, brief_path, direction, end_criteria, status)"
            " VALUES (1, 'built', 'b.md', 'implement', 'c', 'open')"
        )
        self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        conn.close()

    def test_rebuild_restores_triggers_and_indexes(self):
        conn = self._legacy_db()
        db.ensure_initialized(conn)
        names = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE tbl_name='tasks' "
                "AND type IN ('trigger','index')"
            )
        }
        for expected in (
            "trg_tasks_parent_same_lab",
            "trg_tasks_updated_at",
            "trg_tasks_abandon_releases_student",
            "idx_tasks_lab",
            "idx_tasks_status",
        ):
            self.assertIn(expected, names)
        conn.close()

    def test_rebuild_is_a_no_op_on_a_current_db(self):
        conn = db.connect(":memory:")
        db.ensure_initialized(conn)
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='tasks'"
        ).fetchone()[0]
        db.ensure_initialized(conn)
        self.assertEqual(
            conn.execute("SELECT sql FROM sqlite_master WHERE name='tasks'").fetchone()[0], ddl
        )
        conn.close()
