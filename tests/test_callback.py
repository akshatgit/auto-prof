"""Tests for the professor callback (docs/DESIGN.md §3.3)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import callback  # noqa: E402
from autoprof.backends.base import Backend, BackendResult  # noqa: E402
from tests.helpers import fresh_db, seed_lab_with_student  # noqa: E402


class ScriptedBackend(Backend):
    name = "scripted"

    def __init__(self, payload):
        self.result = BackendResult(text=json.dumps(payload) if isinstance(payload, dict) else payload)
        self.calls = []

    def run(self, prompt, **opts):
        self.calls.append(prompt)
        return self.result


def _decision(decision="resolved", **over):
    payload = {
        "decision": decision,
        "rationale": "the end criteria are met",
        "refined_end_criteria": None,
        "parent_closes": True,
        "children": [],
        "nominate": False,
        "nomination_rationale": "",
    }
    payload.update(over)
    return payload


def _enqueue(conn, task_id):
    cur = conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status) "
        "VALUES ('professor_callback', 'task', ?, 'pending')",
        (task_id,),
    )
    conn.commit()
    return cur.lastrowid


def _accepted_paper(conn, ids, title="A Result"):
    cur = conn.execute(
        "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
        "VALUES (?, ?, 'p.html', ?, 'accepted', 1)",
        (ids["task_id"], ids["student_id"], title),
    )
    conn.commit()
    return cur.lastrowid


class RequestCallbackTests(unittest.TestCase):
    def test_enqueues_once_only(self):
        """Two concurrent callbacks on one task could split it twice."""
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        first = callback.request_callback(conn, ids["task_id"])
        second = callback.request_callback(conn, ids["task_id"])
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE kind='professor_callback'"
            ).fetchone()[0],
            1,
        )
        conn.close()


class DecisionTests(unittest.TestCase):
    def _run(self, conn, ids, payload, lab_dir):
        job_id = _enqueue(conn, ids["task_id"])
        backend = ScriptedBackend(payload)
        outcome = callback.execute_professor_callback_job(conn, job_id, backend, lab_dir)
        return outcome, backend

    def test_resolved_completes_the_task(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        _accepted_paper(conn, ids)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            outcome, _ = self._run(conn, ids, _decision("resolved"), lab_dir)
            self.assertEqual(outcome, "done")
            self.assertTrue((lab_dir / f"{ids['lab_id']}/tasks/{ids['task_id']}/decision.md").exists())
        self.assertEqual(
            conn.execute("SELECT status FROM tasks WHERE id=?", (ids["task_id"],)).fetchone()[0],
            "completed",
        )
        conn.close()

    def test_keep_going_refines_criteria_and_requeues_work(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            self._run(
                conn, ids,
                _decision("keep_going", refined_end_criteria="now also settle rank 5"),
                Path(d),
            )
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (ids["task_id"],)).fetchone()
        self.assertEqual(task["status"], "in_progress")
        self.assertEqual(task["end_criteria"], "now also settle rank 5")
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM jobs WHERE status='pending'")]
        self.assertIn("student_work", kinds)
        conn.close()

    def test_keep_going_without_refinement_leaves_criteria_alone(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        before = conn.execute(
            "SELECT end_criteria FROM tasks WHERE id=?", (ids["task_id"],)
        ).fetchone()[0]
        with tempfile.TemporaryDirectory() as d:
            self._run(conn, ids, _decision("keep_going", refined_end_criteria=None), Path(d))
        self.assertEqual(
            conn.execute("SELECT end_criteria FROM tasks WHERE id=?", (ids["task_id"],)).fetchone()[0],
            before,
        )
        conn.close()

    def test_split_creates_children_with_students_and_work(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        children = [
            {"title": "Child A", "direction": "prove", "end_criteria": "settle A", "brief": "b"},
            {"title": "Child B", "direction": "open", "end_criteria": "explore B", "brief": "b"},
        ]
        with tempfile.TemporaryDirectory() as d:
            self._run(conn, ids, _decision("split", children=children), Path(d))

        kids = conn.execute(
            "SELECT * FROM tasks WHERE parent_task_id = ? ORDER BY id", (ids["task_id"],)
        ).fetchall()
        self.assertEqual(len(kids), 2)
        for kid in kids:
            self.assertIsNotNone(kid["assigned_student_id"])
            self.assertEqual(kid["status"], "in_progress")
        self.assertEqual(
            conn.execute("SELECT status FROM tasks WHERE id=?", (ids["task_id"],)).fetchone()[0],
            "completed",
        )
        conn.close()

    def test_split_can_leave_the_parent_open(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        children = [{"title": "C", "direction": "prove", "end_criteria": "e", "brief": "b"}]
        with tempfile.TemporaryDirectory() as d:
            self._run(
                conn, ids, _decision("split", children=children, parent_closes=False), Path(d)
            )
        self.assertEqual(
            conn.execute("SELECT status FROM tasks WHERE id=?", (ids["task_id"],)).fetchone()[0],
            "in_progress",
        )
        conn.close()

    def test_split_with_unusable_children_fails_the_job(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            outcome, _ = self._run(
                conn, ids,
                _decision("split", children=[{"title": "x", "direction": "maybe"}]),
                Path(d),
            )
        self.assertIn(outcome, ("retrying", "failed"))
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM tasks WHERE parent_task_id IS NOT NULL").fetchone()[0],
            0,
        )
        conn.close()

    def test_abandon_closes_the_task_and_releases_the_student(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            self._run(conn, ids, _decision("abandon"), Path(d))
        self.assertEqual(
            conn.execute("SELECT status FROM tasks WHERE id=?", (ids["task_id"],)).fetchone()[0],
            "abandoned",
        )
        # The schema trigger frees the student on abandonment.
        self.assertIsNone(
            conn.execute("SELECT task_id FROM students WHERE id=?", (ids["student_id"],)).fetchone()[0]
        )
        conn.close()

    def test_unknown_decision_fails_the_job(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            outcome, _ = self._run(conn, ids, _decision("ponder"), Path(d))
        self.assertIn(outcome, ("retrying", "failed"))
        self.assertEqual(
            conn.execute("SELECT status FROM tasks WHERE id=?", (ids["task_id"],)).fetchone()[0],
            "open",
        )
        conn.close()

    def test_already_closed_task_is_a_noop(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (ids["task_id"],))
        conn.commit()
        with tempfile.TemporaryDirectory() as d:
            outcome, backend = self._run(conn, ids, _decision("split"), Path(d))
        self.assertEqual(outcome, "done")
        self.assertEqual(backend.calls, [])
        conn.close()


class NominationTests(unittest.TestCase):
    def _run(self, conn, ids, payload, lab_dir):
        job_id = _enqueue(conn, ids["task_id"])
        return callback.execute_professor_callback_job(conn, job_id, ScriptedBackend(payload), lab_dir)

    def test_nomination_sets_the_student_defending(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        _accepted_paper(conn, ids)
        with tempfile.TemporaryDirectory() as d:
            self._run(conn, ids, _decision("resolved", nominate=True,
                                           nomination_rationale="body of work suffices"), Path(d))
        self.assertEqual(
            conn.execute("SELECT status FROM students WHERE id=?", (ids["student_id"],)).fetchone()[0],
            "defending",
        )
        conn.close()

    def test_unnominated_student_is_reassigned_to_an_open_task(self):
        """Closing a task must not leave a student holding finished work."""
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        cur = conn.execute(
            "INSERT INTO tasks (lab_id, title, brief_path, direction, end_criteria, status) "
            "VALUES (?, 'Next Task', 'b.md', 'prove', 'e', 'open')",
            (ids["lab_id"],),
        )
        next_task = cur.lastrowid
        conn.commit()

        with tempfile.TemporaryDirectory() as d:
            self._run(conn, ids, _decision("resolved", nominate=False), Path(d))

        student = conn.execute(
            "SELECT * FROM students WHERE id=?", (ids["student_id"],)
        ).fetchone()
        self.assertEqual(student["task_id"], next_task)
        self.assertEqual(student["status"], "working")
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM jobs WHERE status='pending'")]
        self.assertIn("student_work", kinds)
        conn.close()

    def test_unnominated_student_with_no_open_task_is_unassigned(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            self._run(conn, ids, _decision("resolved", nominate=False), Path(d))
        student = conn.execute(
            "SELECT * FROM students WHERE id=?", (ids["student_id"],)
        ).fetchone()
        self.assertIsNone(student["task_id"])
        self.assertEqual(student["status"], "unassigned")
        conn.close()

    def test_abandon_does_not_nominate(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            self._run(conn, ids, _decision("abandon", nominate=True), Path(d))
        self.assertNotEqual(
            conn.execute("SELECT status FROM students WHERE id=?", (ids["student_id"],)).fetchone()[0],
            "defending",
        )
        conn.close()


class ContextTests(unittest.TestCase):
    def test_prompt_carries_reviewer_rationales_and_cumulative_record(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        paper_id = _accepted_paper(conn, ids, title="Landmark Result")
        rel = f"{ids['lab_id']}/r.md"
        conn.execute(
            "INSERT INTO reviews (target_type,target_id,review_round,reviewer_index,verdict,rationale_path) "
            "VALUES ('paper',?,1,1,'strong_accept',?)",
            (paper_id, rel),
        )
        conn.commit()

        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            (lab_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (lab_dir / rel).write_text("the proof is complete and checkable")

            job_id = _enqueue(conn, ids["task_id"])
            backend = ScriptedBackend(_decision("resolved"))
            callback.execute_professor_callback_job(conn, job_id, backend, lab_dir)

            prompt = backend.calls[0]
            self.assertIn("Landmark Result", prompt)
            self.assertIn("the proof is complete and checkable", prompt)
            self.assertIn("cumulative record", prompt)
        conn.close()


if __name__ == "__main__":
    unittest.main()
