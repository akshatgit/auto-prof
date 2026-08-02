"""Tests for the shared reference bank."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import references  # noqa: E402
from autoprof.backends.base import BackendResult  # noqa: E402
from tests.helpers import fresh_db, seed_lab_with_student  # noqa: E402


def _paper(conn, ids, status="accepted", title="A Result"):
    cur = conn.execute(
        "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
        "VALUES (?, ?, 'p.html', ?, ?, 1)",
        (ids["task_id"], ids["student_id"], title, status),
    )
    conn.commit()
    return cur.lastrowid


class AddReferenceTests(unittest.TestCase):
    def test_same_identifier_converges_on_one_row(self):
        """Two students citing the same arXiv id is normal -- it must
        converge, not fail, and must not create a second row under a
        different title."""
        conn = fresh_db()
        first = references.add_reference(
            conn, "Matroidal Approximations of Independence Systems",
            "de Vries, Vohra", identifier="arXiv:1906.06217",
        )
        second = references.add_reference(
            conn, "A Totally Different Title", "Someone Else", identifier="arXiv:1906.06217",
        )
        self.assertEqual(first, second)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM reference_works").fetchone()[0], 1)
        conn.close()

    def test_title_and_authors_are_required(self):
        conn = fresh_db()
        with self.assertRaises(references.ReferenceError):
            references.add_reference(conn, "  ", "someone")
        with self.assertRaises(references.ReferenceError):
            references.add_reference(conn, "a title", "")
        conn.close()

    def test_new_references_start_unverified(self):
        conn = fresh_db()
        ref_id = references.add_reference(conn, "T", "A")
        row = conn.execute("SELECT * FROM reference_works WHERE id=?", (ref_id,)).fetchone()
        self.assertEqual(row["status"], references.UNVERIFIED)
        conn.close()


class VerificationTests(unittest.TestCase):
    def test_only_verified_works_are_citable(self):
        conn = fresh_db()
        good = references.add_reference(conn, "Real Work", "Real Author", identifier="doi:1")
        references.add_reference(conn, "Unchecked", "Someone", identifier="doi:2")
        references.set_status(conn, good, references.VERIFIED)

        citable = references.citable(conn)
        self.assertEqual([r["id"] for r in citable], [good])
        conn.close()

    def test_disputing_keeps_the_row_and_names_affected_papers(self):
        """The row must survive: papers already cite it, and the citation
        edges are how those papers get found."""
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        paper_id = _paper(conn, ids)
        ref_id = references.add_reference(conn, "Wrong Title", "X", identifier="arXiv:1906.06217")
        references.cite(conn, paper_id, ref_id)

        references.set_status(conn, ref_id, references.DISPUTED, "title does not match the record")

        self.assertIsNotNone(
            conn.execute("SELECT 1 FROM reference_works WHERE id=?", (ref_id,)).fetchone()
        )
        affected = references.contaminated_papers(conn, ref_id)
        self.assertEqual([p["id"] for p in affected], [paper_id])
        conn.close()

    def test_unknown_status_is_rejected(self):
        conn = fresh_db()
        ref_id = references.add_reference(conn, "T", "A")
        with self.assertRaises(references.ReferenceError):
            references.set_status(conn, ref_id, "probably-fine")
        conn.close()


class AcceptedPaperEnrolmentTests(unittest.TestCase):
    def test_accepted_paper_becomes_a_verified_reference(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        paper_id = _paper(conn, ids, title="Exact Spectrum")

        ref_id = references.register_accepted_paper(conn, paper_id)
        row = conn.execute("SELECT * FROM reference_works WHERE id=?", (ref_id,)).fetchone()
        self.assertEqual(row["kind"], "internal_paper")
        self.assertEqual(row["status"], references.VERIFIED)
        self.assertEqual(row["title"], "Exact Spectrum")
        self.assertEqual(row["paper_id"], paper_id)
        conn.close()

    def test_unaccepted_papers_are_not_enrolled(self):
        """A rejected or in-review paper is not established work."""
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        for status in ("in_review", "rejected", "draft"):
            paper_id = _paper(conn, ids, status=status)
            self.assertIsNone(references.register_accepted_paper(conn, paper_id))
        conn.close()

    def test_enrolment_is_idempotent(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        paper_id = _paper(conn, ids)
        first = references.register_accepted_paper(conn, paper_id)
        second = references.register_accepted_paper(conn, paper_id)
        self.assertEqual(first, second)
        conn.close()

    def test_joint_paper_records_every_author(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        paper_id = _paper(conn, ids)
        conn.execute(
            "INSERT INTO paper_authors (paper_id, student_id, author_order) VALUES (?, ?, 1)",
            (paper_id, ids["student_id"]),
        )
        conn.commit()

        ref_id = references.register_accepted_paper(conn, paper_id)
        row = conn.execute("SELECT authors FROM reference_works WHERE id=?", (ref_id,)).fetchone()
        self.assertIn(f"Student {ids['student_id']}", row["authors"])
        conn.close()


class PromptRenderingTests(unittest.TestCase):
    def test_empty_bank_still_forbids_invention(self):
        conn = fresh_db()
        rendered = references.render_for_prompt(conn)
        self.assertIn("Do NOT invent references", rendered)
        conn.close()

    def test_verified_entries_are_listed_with_the_citation_rules(self):
        conn = fresh_db()
        ref_id = references.add_reference(
            conn, "Matroidal Approximations of Independence Systems",
            "de Vries, Vohra", identifier="arXiv:1906.06217",
            venue="Operations Research Letters", year=2020,
        )
        references.set_status(conn, ref_id, references.VERIFIED)

        rendered = references.render_for_prompt(conn)
        self.assertIn("Matroidal Approximations", rendered)
        self.assertIn("arXiv:1906.06217", rendered)
        self.assertIn("fabricated citation", rendered)
        conn.close()

    def test_unverified_entries_are_not_offered(self):
        conn = fresh_db()
        references.add_reference(conn, "Unchecked Work", "Nobody", identifier="doi:9")
        self.assertNotIn("Unchecked Work", references.render_for_prompt(conn))
        conn.close()


class SeedingTests(unittest.TestCase):
    """Seeding at lab creation must contribute CANDIDATES, never citable
    entries -- a model asked for prior art invents plausible ones."""

    class _Backend:
        name = "scripted"

        def __init__(self, text):
            self.result = BackendResult(text=text)

        def run(self, prompt, **opts):
            self.prompt = prompt
            return self.result

    def test_seeded_works_land_unverified_and_uncitable(self):
        conn = fresh_db()
        backend = self._Backend(json.dumps({"works": [
            {"title": "A Real Paper", "authors": "Someone", "venue": "V", "year": 1978,
             "identifier": "doi:10.1/x"},
        ]}))
        ids = references.seed_from_root_problem(conn, backend, "root", "field")

        self.assertEqual(len(ids), 1)
        row = conn.execute("SELECT * FROM reference_works WHERE id=?", (ids[0],)).fetchone()
        self.assertEqual(row["status"], references.UNVERIFIED)
        self.assertEqual(references.citable(conn), [])
        conn.close()

    def test_entries_missing_title_or_authors_are_dropped(self):
        conn = fresh_db()
        backend = self._Backend(json.dumps({"works": [
            {"title": "", "authors": "X"},
            {"title": "Y", "authors": ""},
            {"title": "Good", "authors": "Author"},
        ]}))
        self.assertEqual(len(references.seed_from_root_problem(conn, backend, "r", "f")), 1)
        conn.close()

    def test_unusable_output_seeds_nothing_rather_than_raising(self):
        """A lab that cannot be seeded is still a perfectly good lab."""
        conn = fresh_db()
        for text in ("not json at all", "", '{"works": "wrong shape"}'):
            self.assertEqual(references.seed_from_root_problem(conn, self._Backend(text), "r", "f"), [])
        conn.close()

    def test_backend_error_seeds_nothing(self):
        conn = fresh_db()
        class _Failing:
            name = "x"
            def run(self, prompt, **opts):
                return BackendResult(text="", error="boom")
        self.assertEqual(references.seed_from_root_problem(conn, _Failing(), "r", "f"), [])
        conn.close()


class VerificationJobTests(unittest.TestCase):
    def test_verified_verdict_applies_corrections(self):
        """A real work carrying a wrong title is a correction, not a
        rejection -- that is the case that actually occurred."""
        conn = fresh_db()
        ref_id = references.add_reference(
            conn, "Greedy Algorithms and Rank Quotients", "de Vries, Vohra",
            identifier="arXiv:1906.06217",
        )
        verified, disputed = references.apply_verification(conn, [
            {"id": ref_id, "verdict": "verified",
             "title": "Matroidal Approximations of Independence Systems",
             "venue": "Operations Research Letters", "year": 2020},
        ])
        self.assertEqual((verified, disputed), (1, 0))
        row = conn.execute("SELECT * FROM reference_works WHERE id=?", (ref_id,)).fetchone()
        self.assertEqual(row["title"], "Matroidal Approximations of Independence Systems")
        self.assertEqual(row["status"], references.VERIFIED)
        self.assertIsNotNone(row["verified_at"])
        conn.close()

    def test_disputed_verdict_keeps_it_uncitable(self):
        conn = fresh_db()
        ref_id = references.add_reference(conn, "Invented Work", "Nobody")
        references.apply_verification(conn, [
            {"id": ref_id, "verdict": "disputed", "note": "cannot confirm this exists"},
        ])
        row = conn.execute("SELECT * FROM reference_works WHERE id=?", (ref_id,)).fetchone()
        self.assertEqual(row["status"], references.DISPUTED)
        self.assertEqual(references.citable(conn), [])
        conn.close()

    def test_unknown_ids_and_junk_are_ignored(self):
        conn = fresh_db()
        verified, disputed = references.apply_verification(conn, [
            {"id": 999, "verdict": "verified"},
            {"no_id": True},
            "not a dict",
        ])
        self.assertEqual((verified, disputed), (0, 0))
        conn.close()

    def test_verified_at_is_a_real_timestamp(self):
        conn = fresh_db()
        ref_id = references.add_reference(conn, "T", "A", status=references.VERIFIED)
        stamp = conn.execute(
            "SELECT verified_at FROM reference_works WHERE id=?", (ref_id,)
        ).fetchone()[0]
        self.assertNotIn("datetime", stamp)
        self.assertRegex(stamp, r"^\d{4}-\d{2}-\d{2} ")
        conn.close()


class InternalVsPublishedTests(unittest.TestCase):
    """Internal lab papers must be citable but clearly marked. Rendering
    them alongside published literature made a student cite them as
    published work, and a reviewer -- unable to identify them externally
    -- treated the citations as fabricated."""

    def _both(self, conn):
        ids = seed_lab_with_student(conn)
        references.add_reference(
            conn, "A Published Work", "Real Author", identifier="arXiv:1234",
            venue="A Journal", year=2020, status=references.VERIFIED,
        )
        references.register_accepted_paper(conn, _paper(conn, ids, title="An Internal Result"))
        return ids

    def test_the_two_kinds_are_rendered_separately(self):
        conn = fresh_db()
        self._both(conn)
        rendered = references.render_for_prompt(conn)
        self.assertIn("PUBLISHED PRIOR WORK", rendered)
        self.assertIn("INTERNAL LAB RESULTS", rendered)
        self.assertLess(
            rendered.index("A Published Work"), rendered.index("An Internal Result")
        )
        conn.close()

    def test_students_are_told_to_label_internal_citations(self):
        conn = fresh_db()
        self._both(conn)
        rendered = references.render_for_prompt(conn)
        self.assertIn("not externally published", rendered)
        self.assertIn("treat it as fabricated", rendered)
        conn.close()

    def test_internal_results_carry_no_priority_weight(self):
        conn = fresh_db()
        self._both(conn)
        self.assertIn("Never claim priority", references.render_for_prompt(conn))
        conn.close()

    def test_reviewers_are_told_how_to_treat_internal_citations(self):
        rubric = (Path(__file__).resolve().parent.parent
                  / "templates" / "review_rubric.md").read_text()
        self.assertIn("Internal lab results are a distinct case", rubric)
        self.assertIn("carries no weight as prior", rubric.replace("\n", " "))


if __name__ == "__main__":
    unittest.main()
