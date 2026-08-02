import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
