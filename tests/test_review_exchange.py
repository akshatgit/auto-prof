"""Reviewer <-> author exchanges (autoprof/author_response.py).

The feature exists because the rubric's empirical gate was unreachable by
construction: it demands a falsification test, and nothing ever told the
authors to run one. Three independent reviewers withheld strong_accept
from a paper none of them could break, for exactly that reason.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import author_response, config, paper_review  # noqa: E402
from autoprof.backends.base import Backend, BackendResult  # noqa: E402
from tests.helpers import fresh_db, seed_lab_with_student  # noqa: E402


class ScriptedBackend(Backend):
    name = "scripted"

    def __init__(self, *results):
        self._results = list(results)
        self.calls = []

    def run(self, prompt, **opts):
        self.calls.append(prompt)
        return self._results.pop(0) if self._results else BackendResult(text="VERDICT: accept")


def _seed_paper(conn, ids, lab_dir: Path) -> int:
    cur = conn.execute(
        "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
        "VALUES (?, ?, 'p', 'A Paper', 'in_review', 1)",
        (ids["task_id"], ids["student_id"]),
    )
    pid = cur.lastrowid
    rel = f"{ids['lab_id']}/tasks/{ids['task_id']}/papers/{pid}/paper.html"
    conn.execute("UPDATE papers SET path=? WHERE id=?", (rel, pid))
    conn.execute("UPDATE students SET status='in_review' WHERE id=?", (ids["student_id"],))
    conn.commit()
    f = lab_dir / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("<h1>A Paper</h1>")
    return pid


REQUEST = (
    "REQUEST:\n- run the finite enumeration for n<=5 and report exact output\n\n"
    "VERDICT: weak_accept"
)
_REQUEST_ONLY = "REQUEST:\n- run the finite enumeration"


class ReviewerRequestTests(unittest.TestCase):
    def test_a_request_records_the_verdict_and_queues_the_authors(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            pid = _seed_paper(conn, ids, lab_dir)
            job = paper_review.request_paper_review(conn, pid)[0]
            paper_review.execute_paper_review_job(
                conn, job, ScriptedBackend(BackendResult(text=REQUEST)), lab_dir
            )

        # The verdict stands on the record immediately: if the exchange
        # never completes, this is what counts.
        self.assertEqual(
            conn.execute("SELECT verdict FROM reviews").fetchone()[0], "weak_accept"
        )
        ex = conn.execute("SELECT * FROM review_exchanges").fetchone()
        self.assertEqual(ex["exchange_round"], 1)
        self.assertIsNone(ex["response_path"])
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE kind='author_response' AND status='pending'"
            ).fetchone()[0], 1,
        )
        conn.close()

    def test_a_request_without_a_verdict_is_refused(self):
        # The verdict is not optional. A reviewer that only asks has left
        # the round with no state, so if the exchange fails there is
        # nothing to tally.
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            pid = _seed_paper(conn, ids, lab_dir)
            job = paper_review.request_paper_review(conn, pid)[0]
            outcome = paper_review.execute_paper_review_job(
                conn, job, ScriptedBackend(BackendResult(text=_REQUEST_ONLY)), lab_dir
            )
        self.assertIn(outcome, ("retrying", "failed"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_exchanges").fetchone()[0], 0)
        conn.close()

    def test_a_second_turn_revises_the_verdict_rather_than_filing_another(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            pid = _seed_paper(conn, ids, lab_dir)
            job = paper_review.request_paper_review(conn, pid)[0]
            paper_review.execute_paper_review_job(
                conn, job, ScriptedBackend(BackendResult(text=REQUEST)), lab_dir
            )
            resp = conn.execute(
                "SELECT id FROM jobs WHERE kind='author_response'"
            ).fetchone()["id"]
            author_response.execute_author_response_job(
                conn, resp, ScriptedBackend(BackendResult(text="ran it: 3, 15, 45")), lab_dir
            )
            nxt = conn.execute(
                "SELECT id FROM jobs WHERE kind='paper_review' AND status='pending' "
                "AND reviewer_index=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
            paper_review.execute_paper_review_job(
                conn, nxt, ScriptedBackend(BackendResult(text="VERDICT: strong_accept")), lab_dir
            )

        rows = conn.execute(
            "SELECT verdict FROM reviews WHERE reviewer_index=1"
        ).fetchall()
        self.assertEqual(len(rows), 1, "one verdict per reviewer per round")
        self.assertEqual(rows[0]["verdict"], "strong_accept")
        conn.close()

    def test_the_exchange_loop_is_bounded(self):
        # Every unbounded loop in this system has run away at least once.
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        cap = config.max_review_exchanges()
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            pid = _seed_paper(conn, ids, lab_dir)
            job = paper_review.request_paper_review(conn, pid)[0]
            for _ in range(cap):
                paper_review.execute_paper_review_job(
                    conn, job, ScriptedBackend(BackendResult(text=REQUEST)), lab_dir
                )
                conn.execute("UPDATE jobs SET status='pending', lease_id=NULL WHERE id=?", (job,))
                conn.execute(
                    "UPDATE review_exchanges SET response_path='r' WHERE response_path IS NULL"
                )
                conn.commit()
            # Past the cap the request is ignored and the verdict stands.
            outcome = paper_review.execute_paper_review_job(
                conn, job, ScriptedBackend(BackendResult(text=REQUEST)), lab_dir
            )
        self.assertEqual(outcome, "done")
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM review_exchanges").fetchone()[0], cap
        )
        self.assertEqual(
            conn.execute("SELECT verdict FROM reviews WHERE reviewer_index=1").fetchone()[0],
            "weak_accept",
        )
        conn.close()


class AuthorResponseTests(unittest.TestCase):
    def _to_request(self, conn, ids, lab_dir):
        pid = _seed_paper(conn, ids, lab_dir)
        job = paper_review.request_paper_review(conn, pid)[0]
        paper_review.execute_paper_review_job(
            conn, job, ScriptedBackend(BackendResult(text=REQUEST)), lab_dir
        )
        return pid, conn.execute(
            "SELECT id FROM jobs WHERE kind='author_response'"
        ).fetchone()["id"]

    def test_answering_records_the_response_and_returns_the_reviewers_turn(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            pid, job = self._to_request(conn, ids, lab_dir)
            author_response.execute_author_response_job(
                conn, job, ScriptedBackend(BackendResult(text="I ran it; output was 3, 15, 45.")),
                lab_dir,
            )
            ex = conn.execute("SELECT * FROM review_exchanges").fetchone()
            self.assertIsNotNone(ex["response_path"])
            self.assertIn("3, 15, 45", (lab_dir / ex["response_path"]).read_text())

        # reviewers 2 and 3 still hold their original jobs; reviewer 1 --
        # the one that asked -- must have been handed a second turn.
        turns = conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE kind='paper_review' AND status='pending' "
            "AND reviewer_index=1"
        ).fetchone()[0]
        self.assertEqual(turns, 1, "the reviewer that asked must get its turn back")
        conn.close()

    def test_the_authors_see_the_request_and_may_use_tools(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            _, job = self._to_request(conn, ids, lab_dir)
            backend = ScriptedBackend(BackendResult(text="answer"))
            author_response.execute_author_response_job(conn, job, backend, lab_dir)
        self.assertIn("finite enumeration", backend.calls[0])
        self.assertIn("tool:verify", backend.calls[0])
        conn.close()

    def test_the_reviewer_sees_the_answer_on_its_next_turn(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            pid, job = self._to_request(conn, ids, lab_dir)
            author_response.execute_author_response_job(
                conn, job, ScriptedBackend(BackendResult(text="output was 3, 15, 45")), lab_dir
            )
            nxt = conn.execute(
                "SELECT id FROM jobs WHERE kind='paper_review' AND status='pending' "
                "AND reviewer_index=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
            backend = ScriptedBackend(BackendResult(text="VERDICT: strong_accept"))
            paper_review.execute_paper_review_job(conn, nxt, backend, lab_dir)

        self.assertIn("output was 3, 15, 45", backend.calls[0])
        self.assertIn("You asked", backend.calls[0])
        verdict = conn.execute("SELECT verdict FROM reviews").fetchone()[0]
        self.assertEqual(verdict, "strong_accept")
        conn.close()

    def test_one_reviewers_exchange_is_never_shown_to_another(self):
        # Reviewer independence is the load-bearing assumption of the
        # panel. Leaking reviewer 1's correspondence to reviewer 2 lets
        # the panel converge through the authors as a relay.
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            pid, job = self._to_request(conn, ids, lab_dir)
            author_response.execute_author_response_job(
                conn, job, ScriptedBackend(BackendResult(text="secret answer")), lab_dir
            )
            transcript = paper_review.exchange_transcript(conn, pid, 1, 2, lab_dir)
        self.assertEqual(transcript, "")
        conn.close()


if __name__ == "__main__":
    unittest.main()


class FinalizeGateTests(unittest.TestCase):
    """Recording verdicts immediately creates a hazard the deferred design
    did not have: all three can be on record while one reviewer is still
    corresponding. Tallying then decides the paper on a verdict its own
    author has said it may revise."""

    def test_the_round_is_not_tallied_while_an_exchange_is_open(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            pid = _seed_paper(conn, ids, lab_dir)
            j1, j2, j3 = paper_review.request_paper_review(conn, pid)
            # reviewer 1 asks; reviewers 2 and 3 decide outright
            paper_review.execute_paper_review_job(
                conn, j1, ScriptedBackend(BackendResult(text=REQUEST)), lab_dir
            )
            for j in (j2, j3):
                paper_review.execute_paper_review_job(
                    conn, j, ScriptedBackend(BackendResult(text="VERDICT: strong_accept")), lab_dir
                )

        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0], 3,
            "all three verdicts are on record",
        )
        self.assertEqual(
            conn.execute("SELECT status FROM papers WHERE id=?", (pid,)).fetchone()[0],
            "in_review",
            "but the paper must not be decided while reviewer 1 is still asking",
        )
        conn.close()

    def test_the_round_tallies_once_the_conversation_closes(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            pid = _seed_paper(conn, ids, lab_dir)
            j1, j2, j3 = paper_review.request_paper_review(conn, pid)
            paper_review.execute_paper_review_job(
                conn, j1, ScriptedBackend(BackendResult(text=REQUEST)), lab_dir
            )
            for j in (j2, j3):
                paper_review.execute_paper_review_job(
                    conn, j, ScriptedBackend(BackendResult(text="VERDICT: strong_accept")), lab_dir
                )
            resp = conn.execute("SELECT id FROM jobs WHERE kind='author_response'").fetchone()["id"]
            author_response.execute_author_response_job(
                conn, resp, ScriptedBackend(BackendResult(text="ran it")), lab_dir
            )
            nxt = conn.execute(
                "SELECT id FROM jobs WHERE kind='paper_review' AND status='pending' "
                "AND reviewer_index=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()["id"]
            paper_review.execute_paper_review_job(
                conn, nxt, ScriptedBackend(BackendResult(text="VERDICT: strong_accept")), lab_dir
            )

        self.assertEqual(
            conn.execute("SELECT status FROM papers WHERE id=?", (pid,)).fetchone()[0], "accepted"
        )
        conn.close()
