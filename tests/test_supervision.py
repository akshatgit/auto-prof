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


def _payload(verdict="continue", guidance="fix lemma 2 step 3", *, complete=None):
    complete = verdict == "ready" if complete is None else complete
    return BackendResult(
        text=json.dumps(
            {
                "verdict": verdict,
                "assessment": "complete result" if complete else "partial result only",
                "guidance": guidance,
                "end_criteria_met": complete,
                "completion_evidence": ["artifact and result verified"] if complete else [],
                "remaining_gaps": [] if complete else ["work remains"],
            }
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

    def test_ready_without_completion_evidence_returns_to_research(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            self._run(conn, ids, Path(d), _payload("ready", complete=False))

        row = conn.execute("SELECT verdict FROM supervisions").fetchone()
        self.assertEqual(row["verdict"], "continue")
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM jobs WHERE status='pending'")]
        self.assertEqual(kinds, ["student_work"])
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

    def test_round_cap_abandons_instead_of_forcing_incomplete_write_up(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            with mock.patch.object(
                supervision.config, "max_supervision_rounds", lambda **_: 2
            ):
                self._run(conn, ids, lab_dir, _payload("continue"))
                self._run(conn, ids, lab_dir, _payload("continue"))

        last = conn.execute("SELECT * FROM supervisions ORDER BY round DESC LIMIT 1").fetchone()
        self.assertEqual(last["verdict"], "abandon")
        kinds = {r["kind"] for r in conn.execute("SELECT kind FROM jobs WHERE status='pending'")}
        self.assertNotIn("student_write_paper", kinds)
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
            with mock.patch.object(
                supervision.config, "max_supervision_rounds", lambda **_: 2
            ):
                self._run(conn, ids, lab_dir, _payload("continue"))
                self._run(conn, ids, lab_dir, _payload("continue"))  # abandoned at cap

                conn.execute(
                    "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
                    "VALUES (?, ?, 'p.html', 'Attempt', 'rejected', 1)",
                    (ids["task_id"], ids["student_id"]),
                )
                conn.execute(
                    "UPDATE students SET task_id=?, status='working' WHERE id=?",
                    (ids["task_id"], ids["student_id"]),
                )
                conn.execute(
                    "UPDATE tasks SET status='in_progress', assigned_student_id=? WHERE id=?",
                    (ids["student_id"], ids["task_id"]),
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
            with mock.patch.object(
                supervision.config, "max_supervision_rounds", lambda **_: 3
            ):
                for _ in range(3):
                    self._run(conn, ids, lab_dir, _payload("continue"))

        verdicts = [r["verdict"] for r in conn.execute(
            "SELECT verdict FROM supervisions ORDER BY round"
        )]
        self.assertEqual(verdicts, ["continue", "continue", "abandon"])
        conn.close()

    def test_zero_scoped_cap_never_forces_ready(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            with mock.patch.object(
                supervision.config, "max_supervision_rounds", lambda **_: 0
            ):
                for _ in range(4):
                    self._run(conn, ids, lab_dir, _payload("continue"))

        verdicts = [r["verdict"] for r in conn.execute(
            "SELECT verdict FROM supervisions ORDER BY round"
        )]
        self.assertEqual(verdicts, ["continue"] * 4)
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
            self.assertIn("defensive software-quality research", backend.calls[0])
            self.assertIn("isolated local containers", backend.calls[0])
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

    def test_long_history_keeps_only_recent_meetings_in_full(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            with mock.patch.object(supervision.config, "max_supervision_rounds", lambda **_: 0):
                for i in range(6):
                    job = _enqueue(conn, ids["task_id"])
                    supervision.execute_professor_supervision_job(
                        conn,
                        job,
                        ScriptedBackend(_payload("continue", f"instruction-{i}")),
                        lab_dir,
                    )

            rendered = supervision.render_student_guidance(conn, ids["task_id"], lab_dir)
            self.assertIn("older meetings compacted", rendered)
            self.assertNotIn("instruction-0", rendered)
            self.assertNotIn("instruction-1", rendered)
            self.assertIn("instruction-5", rendered)
        conn.close()


class HistoryScopingTests(unittest.TestCase):
    """A replacement student must not inherit their predecessor's record."""

    def _meeting(self, conn, task_id, student_id, round_, verdict, lab_dir):
        rel = f"1/tasks/{task_id}/supervision/{round_}.md"
        (lab_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (lab_dir / rel).write_text(f"guidance for round {round_}")
        conn.execute(
            "INSERT INTO supervisions (task_id, student_id, round, verdict, guidance_path) "
            "VALUES (?, ?, ?, ?, ?)", (task_id, student_id, round_, verdict, rel))
        conn.commit()

    def test_a_new_student_is_not_shown_the_previous_students_meetings_as_their_own(self):
        # `round` is UNIQUE per task so it never resets. A replacement
        # student's first meeting was numbered 54 and carried 53 rounds of
        # their predecessor's history, including the abandon that freed the
        # task; the professor read its own exhausted patience and abandoned
        # again on the new student's very first round.
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            old = ids["student_id"]
            self._meeting(conn, ids["task_id"], old, 1, "continue", lab_dir)
            self._meeting(conn, ids["task_id"], old, 2, "abandon", lab_dir)
            # students.task_id is UNIQUE: abandon frees the old student first.
            conn.execute("UPDATE students SET task_id=NULL, status='unassigned' WHERE id=?", (old,))
            new = conn.execute(
                "INSERT INTO students (task_id, professor_id, status, memory_path) "
                "VALUES (?, ?, 'working', 'x')",
                (ids["task_id"], ids["professor_id"]),
            ).lastrowid
            conn.commit()

            out = supervision.render_history(conn, ids["task_id"], lab_dir, new)
            self.assertIn("first meeting with this student", out)
            self.assertIn("previous student", out)
            self.assertIn("do not", out.lower())
            # The predecessor's guidance body must not read as this student's.
            self.assertNotIn("guidance for round 1", out)
        conn.close()

    def test_a_students_own_meetings_are_still_shown_in_full(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            self._meeting(conn, ids["task_id"], ids["student_id"], 1, "continue", lab_dir)
            out = supervision.render_history(conn, ids["task_id"], lab_dir, ids["student_id"])
            self.assertIn("guidance for round 1", out)
        conn.close()


if __name__ == "__main__":
    unittest.main()


class PriorReviewFeedbackTests(unittest.TestCase):
    """A professor must see why the last paper was rejected."""

    def setUp(self):
        self.conn = fresh_db()
        self.ids = seed_lab_with_student(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lab_dir = Path(self.tmp.name)

    def tearDown(self):
        self.conn.close()

    def _add_paper_with_reviews(self, verdicts, rationale="the reducer reduces nothing"):
        cur = self.conn.execute(
            "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
            "VALUES (?, ?, 'p.html', 'T', 'rejected', 1)",
            (self.ids["task_id"], self.ids["student_id"]),
        )
        paper_id = cur.lastrowid
        for i, verdict in enumerate(verdicts, start=1):
            rel = f"reviews/{i}.md"
            (self.lab_dir / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.lab_dir / rel).write_text(f"{rationale} ({i})")
            self.conn.execute(
                "INSERT INTO reviews (target_type, target_id, review_round, reviewer_index, "
                "verdict, rationale_path, reviewer_backend) "
                "VALUES ('paper', ?, 1, ?, ?, ?, 'claude')",
                (paper_id, i, verdict, rel),
            )
        self.conn.commit()
        return paper_id

    def test_no_paper_yields_nothing(self):
        self.assertEqual(
            supervision.render_prior_reviews(self.conn, self.ids["task_id"], self.lab_dir), ""
        )

    def test_verdicts_and_rationales_are_shown(self):
        self._add_paper_with_reviews(["reject", "strong_accept", "reject"])
        out = supervision.render_prior_reviews(self.conn, self.ids["task_id"], self.lab_dir)
        self.assertIn("reject", out)
        self.assertIn("strong_accept", out)
        self.assertIn("the reducer reduces nothing", out)
        self.assertIn("must actually", out)

    def test_reviewer_family_is_named(self):
        self._add_paper_with_reviews(["reject"])
        out = supervision.render_prior_reviews(self.conn, self.ids["task_id"], self.lab_dir)
        self.assertIn("claude", out)

    def test_missing_rationale_file_does_not_raise(self):
        paper_id = self._add_paper_with_reviews(["reject"])
        (self.lab_dir / "reviews" / "1.md").unlink()
        out = supervision.render_prior_reviews(self.conn, self.ids["task_id"], self.lab_dir)
        self.assertIn("rationale unavailable", out)

    def test_prompt_template_has_the_slot(self):
        self.assertIn("{prior_reviews}", supervision.SUPERVISION_PROMPT_TEMPLATE)


class ResubmitGuardTests(unittest.TestCase):
    """A rejected paper needs a research round before the next attempt."""

    def setUp(self):
        self.conn = fresh_db()
        self.ids = seed_lab_with_student(self.conn)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lab_dir = Path(self.tmp.name)

    def tearDown(self):
        self.conn.close()

    def _paper(self, status, created_at="2026-08-21 04:00:00"):
        cur = self.conn.execute(
            "INSERT INTO papers (task_id, student_id, path, title, status, review_round, "
            "created_at) VALUES (?, ?, 'p.html', 'T', ?, 1, ?)",
            (self.ids["task_id"], self.ids["student_id"], status, created_at),
        )
        self.conn.commit()
        return cur.lastrowid

    def _meeting(self, verdict, created_at):
        self.conn.execute(
            "INSERT INTO supervisions (task_id, student_id, round, verdict, guidance_path, "
            "created_at) VALUES (?, ?, 99, ?, 'g.md', ?)",
            (self.ids["task_id"], self.ids["student_id"], verdict, created_at),
        )
        self.conn.commit()

    def _run_ready(self):
        payload = {
            "verdict": "ready", "assessment": "done", "guidance": "write it up",
            "end_criteria_met": True, "completion_evidence": ["artifact"],
            "remaining_gaps": [],
        }
        backend = ScriptedBackend(BackendResult(text=json.dumps(payload)))
        job_id = _enqueue(self.conn, self.ids["task_id"])
        supervision.execute_professor_supervision_job(self.conn, job_id, backend, self.lab_dir)
        return self.conn.execute(
            "SELECT verdict FROM supervisions WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (self.ids["task_id"],),
        ).fetchone()["verdict"]

    def test_ready_survives_when_no_paper_exists(self):
        self.assertEqual(self._run_ready(), "ready")

    def test_ready_is_downgraded_straight_after_a_rejection(self):
        self._paper("rejected")
        self.assertEqual(self._run_ready(), "continue")

    def test_ready_survives_after_a_research_round(self):
        self._paper("rejected", created_at="2026-08-21 04:00:00")
        self._meeting("continue", "2026-08-21 04:30:00")
        self.assertEqual(self._run_ready(), "ready")

    def test_an_accepted_paper_does_not_block_a_later_ready(self):
        self._paper("accepted")
        self.assertEqual(self._run_ready(), "ready")


class OperatorNotesTests(unittest.TestCase):
    """Operator instructions must survive the student rewriting its memory."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lab_dir = Path(self.tmp.name)
        self.path = self.lab_dir / "9" / "tasks" / "35" / "OPERATOR_NOTES.md"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def test_absent_file_yields_nothing(self):
        self.assertEqual(supervision.render_operator_notes(self.lab_dir, 9, 35), "")

    def test_empty_file_yields_nothing(self):
        self.path.write_text("   \n")
        self.assertEqual(supervision.render_operator_notes(self.lab_dir, 9, 35), "")

    def test_contents_are_rendered_with_precedence_stated(self):
        self.path.write_text("Run the v5 campaign before writing.")
        out = supervision.render_operator_notes(self.lab_dir, 9, 35)
        self.assertIn("Run the v5 campaign", out)
        self.assertIn("outrank", out)
        self.assertIn("cannot edit them", out)

    def test_notes_are_per_task(self):
        self.path.write_text("for task 35")
        self.assertEqual(supervision.render_operator_notes(self.lab_dir, 9, 36), "")

    def test_prompt_templates_carry_the_slot(self):
        from autoprof import paper
        self.assertIn("{operator_notes}", paper.WORK_PROMPT_TEMPLATE)
        self.assertIn("{operator_notes}", paper.PAPER_PROMPT_TEMPLATE)
