"""Tests for multi-student collaboration on one joint paper."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import collaboration  # noqa: E402
from autoprof.backends.base import Backend, BackendResult  # noqa: E402
from tests.helpers import fresh_db, seed_lab_with_student  # noqa: E402


class ScriptedBackend(Backend):
    name = "scripted"

    def __init__(self, result):
        self.result = result
        self.calls = []

    def run(self, prompt, **opts):
        self.calls.append(prompt)
        return self.result


def _seed_three_students(conn):
    """One lab, three tasks, three students -- the shape that produced
    papers 1-3 in the live run."""
    ids = seed_lab_with_student(conn)
    extra = []
    for n in (2, 3):
        cur = conn.execute(
            "INSERT INTO tasks (lab_id, title, brief_path, direction, end_criteria, status) "
            "VALUES (?, ?, 'b.md', 'prove', 'done', 'in_progress')",
            (ids["lab_id"], f"Task {n}"),
        )
        task_id = cur.lastrowid
        cur = conn.execute(
            "INSERT INTO students (task_id, professor_id, status, memory_path) "
            "VALUES (?, ?, 'working', ?)",
            (task_id, ids["professor_id"], f"{ids['lab_id']}/students/{n}/memory.md"),
        )
        extra.append(cur.lastrowid)
    conn.commit()
    ids["other_students"] = extra
    return ids


def _synthesis(verdict="continue", shared="merged joint state"):
    return BackendResult(
        text=json.dumps({"verdict": verdict, "shared_memory": shared, "guidance": "next steps"})
    )


class FormCollaborationTests(unittest.TestCase):
    def test_forming_seeds_shared_memory_and_starts_round_one(self):
        conn = fresh_db()
        ids = _seed_three_students(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            collab_id = collaboration.form_collaboration(
                conn,
                ids["task_id"],
                [ids["student_id"]] + ids["other_students"],
                "combine the three results into one paper",
                lab_dir,
            )
            row = conn.execute("SELECT * FROM collaborations WHERE id=?", (collab_id,)).fetchone()
            self.assertEqual(row["status"], "working")
            self.assertEqual(row["round"], 1)
            self.assertTrue((lab_dir / row["memory_path"]).exists())

        # one contribution job per member
        jobs_ = conn.execute("SELECT * FROM jobs WHERE kind='collaboration_round'").fetchall()
        self.assertEqual(len(jobs_), 3)
        self.assertTrue(all(j["review_round"] == 1 for j in jobs_))
        conn.close()

    def test_anchor_tasks_student_is_the_lead(self):
        conn = fresh_db()
        ids = _seed_three_students(conn)
        with tempfile.TemporaryDirectory() as d:
            collab_id = collaboration.form_collaboration(
                conn, ids["task_id"], [ids["student_id"]] + ids["other_students"], "g", Path(d)
            )
        lead = conn.execute(
            "SELECT student_id FROM collaboration_members WHERE collaboration_id=? AND role='lead'",
            (collab_id,),
        ).fetchone()
        self.assertEqual(lead["student_id"], ids["student_id"])
        conn.close()

    def test_lead_must_be_among_the_collaborators(self):
        conn = fresh_db()
        ids = _seed_three_students(conn)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(collaboration.CollaborationError):
                collaboration.form_collaboration(
                    conn, ids["task_id"], ids["other_students"], "g", Path(d)
                )
        conn.close()

    def test_two_students_minimum(self):
        conn = fresh_db()
        ids = _seed_three_students(conn)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(collaboration.CollaborationError):
                collaboration.form_collaboration(
                    conn, ids["task_id"], [ids["student_id"]], "g", Path(d)
                )
        conn.close()

    def test_one_collaboration_per_anchor_task(self):
        conn = fresh_db()
        ids = _seed_three_students(conn)
        members = [ids["student_id"]] + ids["other_students"]
        with tempfile.TemporaryDirectory() as d:
            collaboration.form_collaboration(conn, ids["task_id"], members, "g", Path(d))
            with self.assertRaises(collaboration.CollaborationError):
                collaboration.form_collaboration(conn, ids["task_id"], members, "g", Path(d))
        conn.close()

    def test_only_one_lead_is_allowed(self):
        conn = fresh_db()
        ids = _seed_three_students(conn)
        with tempfile.TemporaryDirectory() as d:
            collab_id = collaboration.form_collaboration(
                conn, ids["task_id"], [ids["student_id"]] + ids["other_students"], "g", Path(d)
            )
        with self.assertRaises(Exception):
            conn.execute(
                "INSERT INTO collaboration_members (collaboration_id, student_id, role) "
                "VALUES (?, ?, 'lead')",
                (collab_id, ids["other_students"][0]),
            )
        conn.close()


class CollaborationRoundTests(unittest.TestCase):
    def _form(self, conn, ids, lab_dir):
        return collaboration.form_collaboration(
            conn, ids["task_id"], [ids["student_id"]] + ids["other_students"], "combine", lab_dir
        )

    def _run_all(self, conn, lab_dir, text="my contribution"):
        backends = []
        for job in conn.execute(
            "SELECT id FROM jobs WHERE kind='collaboration_round' AND status='pending' ORDER BY id"
        ).fetchall():
            backend = ScriptedBackend(BackendResult(text=text))
            collaboration.execute_collaboration_round_job(conn, job["id"], backend, lab_dir)
            backends.append(backend)
        return backends

    def test_each_member_contributes_and_synthesis_fires_once(self):
        conn = fresh_db()
        ids = _seed_three_students(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            self._form(conn, ids, lab_dir)
            self._run_all(conn, lab_dir)

            contributions = conn.execute("SELECT * FROM collaboration_contributions").fetchall()
            self.assertEqual(len(contributions), 3)
            for row in contributions:
                self.assertTrue((lab_dir / row["path"]).exists())

        synth = conn.execute("SELECT * FROM jobs WHERE kind='collaboration_synthesis'").fetchall()
        self.assertEqual(len(synth), 1, "synthesis must fire exactly once per round")
        conn.close()

    def test_second_round_shows_co_author_contributions(self):
        conn = fresh_db()
        ids = _seed_three_students(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            self._form(conn, ids, lab_dir)
            self._run_all(conn, lab_dir, text="round one point")

            synth_id = conn.execute(
                "SELECT id FROM jobs WHERE kind='collaboration_synthesis'"
            ).fetchone()["id"]
            collaboration.execute_collaboration_synthesis_job(
                conn, synth_id, ScriptedBackend(_synthesis("continue")), lab_dir
            )

            backends = self._run_all(conn, lab_dir, text="round two point")
            # Each member must see the OTHERS' round-1 work, not their own.
            self.assertIn("round one point", backends[0].calls[0])
            self.assertIn("co_author_contributions", backends[0].calls[0])
        conn.close()

    def test_ready_queues_the_joint_write_up(self):
        conn = fresh_db()
        ids = _seed_three_students(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            collab_id = self._form(conn, ids, lab_dir)
            self._run_all(conn, lab_dir)
            synth_id = conn.execute(
                "SELECT id FROM jobs WHERE kind='collaboration_synthesis'"
            ).fetchone()["id"]
            collaboration.execute_collaboration_synthesis_job(
                conn, synth_id, ScriptedBackend(_synthesis("ready")), lab_dir
            )

            row = conn.execute("SELECT * FROM collaborations WHERE id=?", (collab_id,)).fetchone()
            self.assertEqual(row["status"], "writing")
            self.assertIn("merged joint state", (lab_dir / row["memory_path"]).read_text())
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM jobs WHERE status='pending'")]
        self.assertIn("collaboration_write_paper", kinds)
        conn.close()

    def test_abandon_closes_the_collaboration(self):
        conn = fresh_db()
        ids = _seed_three_students(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            collab_id = self._form(conn, ids, lab_dir)
            self._run_all(conn, lab_dir)
            synth_id = conn.execute(
                "SELECT id FROM jobs WHERE kind='collaboration_synthesis'"
            ).fetchone()["id"]
            collaboration.execute_collaboration_synthesis_job(
                conn, synth_id, ScriptedBackend(_synthesis("abandon")), lab_dir
            )
        self.assertEqual(
            conn.execute("SELECT status FROM collaborations WHERE id=?", (collab_id,)).fetchone()[0],
            "abandoned",
        )
        conn.close()

    def test_empty_shared_memory_is_refused(self):
        """The synthesis replaces shared state wholesale; an empty merge
        would erase the joint work."""
        conn = fresh_db()
        ids = _seed_three_students(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            self._form(conn, ids, lab_dir)
            self._run_all(conn, lab_dir)
            synth_id = conn.execute(
                "SELECT id FROM jobs WHERE kind='collaboration_synthesis'"
            ).fetchone()["id"]
            outcome = collaboration.execute_collaboration_synthesis_job(
                conn, synth_id, ScriptedBackend(_synthesis("continue", shared="  ")), lab_dir
            )
        self.assertIn(outcome, ("retrying", "failed"))
        conn.close()

    def test_round_cap_writes_up_rather_than_discarding(self):
        conn = fresh_db()
        ids = _seed_three_students(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            collab_id = self._form(conn, ids, lab_dir)
            with mock.patch.object(collaboration.config, "max_collaboration_rounds", lambda: 1):
                self._run_all(conn, lab_dir)
                synth_id = conn.execute(
                    "SELECT id FROM jobs WHERE kind='collaboration_synthesis'"
                ).fetchone()["id"]
                collaboration.execute_collaboration_synthesis_job(
                    conn, synth_id, ScriptedBackend(_synthesis("continue")), lab_dir
                )
        self.assertEqual(
            conn.execute("SELECT status FROM collaborations WHERE id=?", (collab_id,)).fetchone()[0],
            "writing",
        )
        conn.close()


class AuthorshipTests(unittest.TestCase):
    def test_byline_is_lead_first_then_co_authors(self):
        conn = fresh_db()
        ids = _seed_three_students(conn)
        with tempfile.TemporaryDirectory() as d:
            collab_id = collaboration.form_collaboration(
                conn, ids["task_id"], [ids["student_id"]] + ids["other_students"], "g", Path(d)
            )
        cur = conn.execute(
            "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
            "VALUES (?, ?, 'p.html', 'Joint', 'in_review', 1)",
            (ids["task_id"], ids["student_id"]),
        )
        paper_id = cur.lastrowid

        authors = collaboration.record_authors(conn, paper_id, collab_id)
        self.assertEqual(authors[0], ids["student_id"])  # lead first
        self.assertEqual(len(authors), 3)
        self.assertEqual(collaboration.authors_for(conn, paper_id), authors)
        conn.close()


if __name__ == "__main__":
    unittest.main()
