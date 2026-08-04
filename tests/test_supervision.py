"""Tests for the student<->professor supervision loop."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import supervision  # noqa: E402
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


def _payload(verdict="continue", guidance="fix lemma 2 step 3"):
    return BackendResult(
        text=json.dumps(
            {"verdict": verdict, "assessment": "partial result only", "guidance": guidance}
        )
    )


def _enqueue(conn, task_id):
    cur = conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status) "
        "VALUES ('professor_supervision', 'task', ?, 'pending')",
        (task_id,),
    )
    conn.commit()
    return cur.lastrowid


class SupervisionVerdictTests(unittest.TestCase):
    def _run(self, conn, ids, lab_dir, result):
        job_id = _enqueue(conn, ids["task_id"])
        backend = ScriptedBackend(result)
        outcome = supervision.execute_professor_supervision_job(conn, job_id, backend, lab_dir)
        return outcome, backend

    def test_continue_sends_the_student_back_to_work(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            outcome, _ = self._run(conn, ids, lab_dir, _payload("continue"))
            self.assertEqual(outcome, "done")

            row = conn.execute("SELECT * FROM supervisions").fetchone()
            self.assertEqual(row["round"], 1)
            self.assertEqual(row["verdict"], "continue")
            self.assertTrue((lab_dir / row["guidance_path"]).exists())
            self.assertIn("fix lemma 2 step 3", (lab_dir / row["guidance_path"]).read_text())

        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM jobs WHERE status='pending'")]
        self.assertEqual(kinds, ["student_work"])
        self.assertEqual(
            conn.execute("SELECT status FROM students WHERE id=?", (ids["student_id"],)).fetchone()[0],
            "working",
        )
        conn.close()

    def test_ready_moves_on_to_the_write_up(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            self._run(conn, ids, Path(d), _payload("ready", "state assumptions explicitly"))

        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM jobs WHERE status='pending'")]
        self.assertEqual(kinds, ["student_write_paper"])
        self.assertEqual(
            conn.execute("SELECT status FROM students WHERE id=?", (ids["student_id"],)).fetchone()[0],
            "writing_paper",
        )
        conn.close()

    def test_abandon_closes_the_task_and_queues_nothing(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            self._run(conn, ids, Path(d), _payload("abandon", "approach cannot work"))

        self.assertEqual(
            conn.execute("SELECT status FROM tasks WHERE id=?", (ids["task_id"],)).fetchone()[0],
            "abandoned",
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM jobs WHERE status='pending'").fetchone()[0], 0
        )
        conn.close()

    def test_unknown_verdict_fails_the_job(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            outcome, _ = self._run(conn, ids, Path(d), _payload("maybe"))
        self.assertIn(outcome, ("retrying", "failed"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM supervisions").fetchone()[0], 0)
        conn.close()

    def test_unparseable_output_fails_the_job(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            outcome, _ = self._run(conn, ids, Path(d), BackendResult(text="I think it's fine"))
        self.assertIn(outcome, ("retrying", "failed"))
        conn.close()

    def test_rounds_increment_across_meetings(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            for _ in range(3):
                self._run(conn, ids, lab_dir, _payload("continue"))
        rounds = [r["round"] for r in conn.execute("SELECT round FROM supervisions ORDER BY round")]
        self.assertEqual(rounds, [1, 2, 3])
        conn.close()

    def test_round_cap_forces_a_write_up_rather_than_discarding_work(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            with mock.patch.object(supervision.config, "max_supervision_rounds", lambda: 2):
                self._run(conn, ids, lab_dir, _payload("continue"))
                self._run(conn, ids, lab_dir, _payload("continue"))

        last = conn.execute("SELECT * FROM supervisions ORDER BY round DESC LIMIT 1").fetchone()
        self.assertEqual(last["verdict"], "ready")
        kinds = {r["kind"] for r in conn.execute("SELECT kind FROM jobs WHERE status='pending'")}
        self.assertIn("student_write_paper", kinds)
        conn.close()

    def test_the_cap_resets_once_a_paper_has_been_written(self):
        # `round` is cumulative and can never reset -- it is UNIQUE per
        # task and names the artifact file. Measuring the cap against it
        # meant that once task #4 passed the cap the condition stayed true
        # forever: 28 consecutive meetings were force-resolved to 'ready',
        # the professor could never say 'continue' again, and the student
        # stopped researching and only re-drafted. Each new attempt at the
        # problem must get a fresh supervision budget.
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            with mock.patch.object(supervision.config, "max_supervision_rounds", lambda: 2):
                self._run(conn, ids, lab_dir, _payload("continue"))
                self._run(conn, ids, lab_dir, _payload("continue"))  # forced 'ready'

                conn.execute(
                    "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
                    "VALUES (?, ?, 'p.html', 'Attempt', 'rejected', 1)",
                    (ids["task_id"], ids["student_id"]),
                )
                conn.execute(
                    "UPDATE students SET status='working' WHERE id=?", (ids["student_id"],)
                )
                conn.commit()

                self._run(conn, ids, lab_dir, _payload("continue"))

        last = conn.execute("SELECT * FROM supervisions ORDER BY round DESC LIMIT 1").fetchone()
        self.assertEqual(last["verdict"], "continue")   # honoured, not forced
        self.assertEqual(last["round"], 3)              # round itself still monotonic
        conn.close()

    def test_the_cap_still_binds_within_one_attempt(self):
        # Resetting per attempt must not make the cap unreachable.
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            with mock.patch.object(supervision.config, "max_supervision_rounds", lambda: 3):
                for _ in range(3):
                    self._run(conn, ids, lab_dir, _payload("continue"))

        verdicts = [r["verdict"] for r in conn.execute(
            "SELECT verdict FROM supervisions ORDER BY round"
        )]
        self.assertEqual(verdicts, ["continue", "continue", "ready"])
        conn.close()


class SupervisionContextTests(unittest.TestCase):
    def test_professor_prompt_carries_prior_meetings(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            job = _enqueue(conn, ids["task_id"])
            supervision.execute_professor_supervision_job(
                conn, job, ScriptedBackend(_payload("continue", "first instruction")), lab_dir
            )
            job2 = _enqueue(conn, ids["task_id"])
            backend = ScriptedBackend(_payload("continue", "second instruction"))
            supervision.execute_professor_supervision_job(conn, job2, backend, lab_dir)

            self.assertIn("first instruction", backend.calls[0])
            self.assertIn("meeting number 2", backend.calls[0])
        conn.close()

    def test_student_guidance_foregrounds_the_latest(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            for text in ("older point", "newest point"):
                job = _enqueue(conn, ids["task_id"])
                supervision.execute_professor_supervision_job(
                    conn, job, ScriptedBackend(_payload("continue", text)), lab_dir
                )

            rendered = supervision.render_student_guidance(conn, ids["task_id"], lab_dir)
            self.assertIn("newest point", rendered)
            self.assertIn("older point", rendered)
            self.assertLess(rendered.index("newest point"), rendered.index("older point"))
        conn.close()

    def test_no_meetings_yet_is_stated_plainly(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            self.assertIn(
                "not met", supervision.render_student_guidance(conn, ids["task_id"], Path(d))
            )
        conn.close()


if __name__ == "__main__":
    unittest.main()
