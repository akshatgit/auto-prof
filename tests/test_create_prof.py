import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from autoprof import create_prof, db
from autoprof.backends.base import Backend, BackendResult
from autoprof.create_prof import SoulGenerationError, generate_soul, persist_professor
from tests.helpers import fresh_db


class FakeBackend(Backend):
    name = "fake"

    def __init__(self, result: BackendResult):
        self._result = result

    def run(self, prompt: str, **opts) -> BackendResult:
        self.last_prompt = prompt
        return self._result


VALID_SOUL = {"name": "Dr. Test", "field": "Testing", "root_problem": "Is testing hard?"}


class GenerateSoulTests(unittest.TestCase):
    def test_success(self):
        backend = FakeBackend(BackendResult(text=json.dumps(VALID_SOUL)))
        soul = generate_soul("an idea", backend)
        self.assertEqual(soul, VALID_SOUL)

    def test_strips_markdown_fences(self):
        fenced = "```json\n" + json.dumps(VALID_SOUL) + "\n```"
        backend = FakeBackend(BackendResult(text=fenced))
        soul = generate_soul("an idea", backend)
        self.assertEqual(soul, VALID_SOUL)

    def test_idea_is_embedded_in_prompt(self):
        backend = FakeBackend(BackendResult(text=json.dumps(VALID_SOUL)))
        generate_soul("a very specific idea about turtles", backend)
        self.assertIn("a very specific idea about turtles", backend.last_prompt)

    def test_missing_required_key_raises(self):
        backend = FakeBackend(BackendResult(text=json.dumps({"name": "X", "field": "Y"})))
        with self.assertRaises(SoulGenerationError):
            generate_soul("an idea", backend)

    def test_non_json_response_raises(self):
        backend = FakeBackend(BackendResult(text="not json"))
        with self.assertRaises(SoulGenerationError):
            generate_soul("an idea", backend)

    def test_backend_error_raises(self):
        backend = FakeBackend(BackendResult(text="", error="boom"))
        with self.assertRaises(SoulGenerationError) as ctx:
            generate_soul("an idea", backend)
        self.assertIn("boom", str(ctx.exception))

    def test_backend_rate_limited_raises_with_useful_message(self):
        backend = FakeBackend(BackendResult(text="", rate_limited=True, retry_after_seconds=42.0))
        with self.assertRaises(SoulGenerationError) as ctx:
            generate_soul("an idea", backend)
        self.assertIn("42", str(ctx.exception))


class PersistProfessorTests(unittest.TestCase):
    def test_creates_rows_and_memory_file(self):
        conn = fresh_db()
        with tempfile.TemporaryDirectory() as d:
            lab_dir = Path(d)
            professor_id, lab_id = persist_professor(
                conn, "Dr. Test", "Testing", "Is testing hard?", lab_dir
            )

            prof_row = conn.execute("SELECT * FROM professors WHERE id=?", (professor_id,)).fetchone()
            lab_row = conn.execute("SELECT * FROM labs WHERE id=?", (lab_id,)).fetchone()

            self.assertEqual(prof_row["name"], "Dr. Test")
            self.assertEqual(prof_row["lab_id"], lab_id)
            self.assertEqual(lab_row["professor_id"], professor_id)
            self.assertEqual(lab_row["root_problem"], "Is testing hard?")

            memory_path = lab_dir / str(lab_id) / "professors" / str(professor_id) / "memory.md"
            self.assertTrue(memory_path.exists())
            self.assertIn("Is testing hard?", memory_path.read_text())
        conn.close()


ScriptedBackend = FakeBackend


class AutoRequestsLabReviewTests(unittest.TestCase):
    """create-prof must leave the lab actually reviewable, not just
    'pending_review' with nothing queued -- see create_prof.run()."""

    def _args(self, tmp, **over):
        import argparse as _argparse

        base = dict(
            idea="is testing hard?",
            yes=True,
            dry_run=False,
            no_review=False,
            no_references=True,
            db_path=Path(tmp) / "autoprof.db",
            lab_dir=Path(tmp) / "lab",
            config_path=Path(tmp) / "missing.toml",
        )
        base.update(over)
        return _argparse.Namespace(**base)

    def _patched_registry(self):
        class _Reg:
            def get_backend(self, kind):
                return ScriptedBackend(
                    BackendResult(
                        text=json.dumps(
                            {"name": "P", "field": "F", "root_problem": "Is testing hard?"}
                        )
                    )
                )

        return _Reg()

    def test_review_jobs_are_enqueued_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self._args(tmp)
            with mock.patch.object(create_prof, "default_registry", lambda _p: self._patched_registry()):
                rc = create_prof.run(args)
            self.assertEqual(rc, 0)

            conn = db.connect(args.db_path)
            jobs = conn.execute("SELECT * FROM jobs WHERE kind='lab_review'").fetchall()
            self.assertEqual(len(jobs), 3)
            self.assertEqual(sorted(j["reviewer_index"] for j in jobs), [1, 2, 3])
            lab = conn.execute("SELECT * FROM labs").fetchone()
            self.assertEqual(lab["status"], "pending_review")
            conn.close()

    def test_no_review_flag_leaves_the_queue_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = self._args(tmp, no_review=True)
            with mock.patch.object(create_prof, "default_registry", lambda _p: self._patched_registry()):
                rc = create_prof.run(args)
            self.assertEqual(rc, 0)

            conn = db.connect(args.db_path)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='lab_review'").fetchone()[0], 0
            )
            conn.close()


if __name__ == "__main__":
    unittest.main()
