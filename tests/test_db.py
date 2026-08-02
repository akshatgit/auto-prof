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
