import tempfile
import unittest
from pathlib import Path

from autoprof.artifacts import write_artifact


class WriteArtifactTests(unittest.TestCase):
    def test_writes_content_creating_parent_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "a" / "b" / "draft.md"
            write_artifact(path, "hello")
            self.assertEqual(path.read_text(), "hello")

    def test_overwrite_is_deterministic_no_duplicate_files(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "draft.md"
            write_artifact(path, "round 1")
            write_artifact(path, "round 2")
            self.assertEqual(path.read_text(), "round 2")
            # exactly one file in the directory -- no accumulated duplicates
            self.assertEqual(list(Path(d).iterdir()), [path])

    def test_no_leftover_temp_files_after_success(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "draft.md"
            write_artifact(path, "content")
            leftovers = [p for p in Path(d).iterdir() if p != path]
            self.assertEqual(leftovers, [])

    def test_write_failure_does_not_clobber_existing_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "draft.md"
            write_artifact(path, "original")

            class Boom(Exception):
                pass

            def bad_writer(f):
                raise Boom("disk full")

            with self.assertRaises(Boom):
                write_artifact(path, "new content", _writer=bad_writer)

            self.assertEqual(path.read_text(), "original")
            leftovers = [p for p in Path(d).iterdir() if p != path]
            self.assertEqual(leftovers, [], "failed write must not leave a temp file behind")


if __name__ == "__main__":
    unittest.main()
