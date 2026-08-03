"""Tests for the assumption ledger (first-principles discipline)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import assumptions  # noqa: E402
from tests.helpers import fresh_db, seed_lab_with_student  # noqa: E402


class ParseBlocksTests(unittest.TestCase):
    def test_parses_a_full_block(self):
        text = (
            "Some prose.\n"
            "```assume\n"
            "statement: every pure system with defect below 1/2 is a matroid\n"
            "source: derived\n"
            "status: derived\n"
            "evidence: section 3, minimal-distance argument\n"
            "```\n"
        )
        [entry] = assumptions.parse_blocks(text)
        self.assertEqual(entry["source"], "derived")
        self.assertEqual(entry["status"], "derived")
        self.assertIn("matroid", entry["statement"])
        self.assertIn("section 3", entry["evidence"])

    def test_defaults_are_the_cautious_ones(self):
        """An entry that omits provenance is inherited-and-unexamined, not
        derived -- the default must not flatter the work."""
        [entry] = assumptions.parse_blocks("```assume\nstatement: X holds\n```")
        self.assertEqual(entry["source"], "inherited")
        self.assertEqual(entry["status"], "assumed")

    def test_unknown_values_fall_back_rather_than_crash(self):
        [entry] = assumptions.parse_blocks(
            "```assume\nstatement: X\nsource: vibes\nstatus: probably\n```"
        )
        self.assertEqual(entry["source"], "inherited")
        self.assertEqual(entry["status"], "assumed")

    def test_malformed_blocks_are_skipped_not_fatal(self):
        """Losing a ledger entry is cheap; losing a completed research
        pass to a missing colon is not."""
        text = "```assume\nno colons here\n```\n```assume\nstatement: real one\n```"
        entries = assumptions.parse_blocks(text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["statement"], "real one")

    def test_no_blocks_is_empty(self):
        self.assertEqual(assumptions.parse_blocks("just prose"), [])
        self.assertEqual(assumptions.parse_blocks(""), [])


class RecordTests(unittest.TestCase):
    def _record(self, conn, ids, entries):
        return assumptions.record(
            conn, entries, lab_id=ids["lab_id"], task_id=ids["task_id"],
            student_id=ids["student_id"],
        )

    def test_revisiting_an_assumption_updates_it(self):
        """assumed -> verified across rounds is the whole point; it must
        not accumulate near-duplicates."""
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        first = self._record(conn, ids, [
            {"statement": "the bound is tight", "source": "brief", "status": "assumed",
             "evidence": None},
        ])
        second = self._record(conn, ids, [
            {"statement": "the bound is tight", "source": "derived", "status": "refuted",
             "evidence": "counterexample at rank 5"},
        ])
        self.assertEqual(first, second)
        row = conn.execute("SELECT * FROM assumptions").fetchone()
        self.assertEqual(row["status"], "refuted")
        self.assertIn("counterexample", row["evidence"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM assumptions").fetchone()[0], 1)
        conn.close()

    def test_ledger_puts_refuted_and_unexamined_first(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        self._record(conn, ids, [
            {"statement": "settled thing", "source": "derived", "status": "derived", "evidence": None},
            {"statement": "false thing", "source": "brief", "status": "refuted", "evidence": None},
            {"statement": "unchecked thing", "source": "brief", "status": "assumed", "evidence": None},
        ])
        order = [r["statement"] for r in assumptions.ledger(conn, ids["task_id"])]
        self.assertEqual(order[0], "false thing")
        self.assertEqual(order[1], "unchecked thing")
        conn.close()

    def test_dependents_names_the_blast_radius(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        conn.execute(
            "INSERT INTO papers (task_id, student_id, path, title, status, review_round) "
            "VALUES (?, ?, 'p.html', 'Built On It', 'accepted', 1)",
            (ids["task_id"], ids["student_id"]),
        )
        conn.commit()
        [aid] = self._record(conn, ids, [
            {"statement": "load-bearing premise", "source": "brief", "status": "refuted",
             "evidence": None},
        ])
        deps = assumptions.dependents(conn, aid)
        self.assertEqual([p["title"] for p in deps["papers"]], ["Built On It"])
        conn.close()


class RenderTests(unittest.TestCase):
    def _seed(self, conn, ids, entries):
        assumptions.record(conn, entries, lab_id=ids["lab_id"], task_id=ids["task_id"],
                           student_id=ids["student_id"])

    def test_empty_ledger_is_itself_challenged_to_the_professor(self):
        """'No assumptions' is never true -- the supervisor should push."""
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        text = assumptions.render(conn, ids["task_id"], for_professor=True)
        self.assertIn("registered NO assumptions", text)
        self.assertIn("worth challenging", text)
        conn.close()

    def test_professor_is_told_to_challenge_an_unexamined_entry(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        self._seed(conn, ids, [
            {"statement": "inherited framing", "source": "brief", "status": "assumed",
             "evidence": None},
        ])
        text = assumptions.render(conn, ids["task_id"], for_professor=True)
        self.assertIn("Challenge at least", text)
        self.assertIn("inherited framing", text)
        conn.close()

    def test_student_is_told_to_derive_verify_or_declare_conditional(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        self._seed(conn, ids, [
            {"statement": "unchecked", "source": "brief", "status": "assumed", "evidence": None},
        ])
        text = assumptions.render(conn, ids["task_id"])
        self.assertIn("verify it", text)
        self.assertIn("conditional", text)
        conn.close()

    def test_refuted_assumptions_are_flagged_to_both(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        self._seed(conn, ids, [
            {"statement": "false premise", "source": "brief", "status": "refuted", "evidence": None},
        ])
        self.assertIn("unsound", assumptions.render(conn, ids["task_id"], for_professor=True))
        self.assertIn("still depends on it", assumptions.render(conn, ids["task_id"]))
        conn.close()

    def test_docs_say_inherited_is_not_derived(self):
        self.assertIn("is `inherited`/`assumed`, not `derived`", assumptions.ASSUMPTION_DOCS)
        self.assertIn("first principles", assumptions.ASSUMPTION_DOCS.lower())


if __name__ == "__main__":
    unittest.main()
