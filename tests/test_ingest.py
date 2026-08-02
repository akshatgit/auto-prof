"""Tests for uploading founder research to bootstrap a lab."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import ingest  # noqa: E402
from tests.helpers import fresh_db, seed_lab_with_student  # noqa: E402


class ExtractTextTests(unittest.TestCase):
    def _write(self, tmp, name, body):
        path = Path(tmp) / name
        path.write_text(body)
        return path

    def test_plain_text_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("a.md", "b.txt", "c.tex", "d.rst"):
                path = self._write(tmp, name, "# Title\n\nbody text")
                self.assertIn("body text", ingest.extract_text(path))

    def test_html_is_stripped_to_prose(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                tmp, "p.html",
                "<html><head><style>body{color:red}</style>"
                "<script>evil()</script></head><body><h1>Real Title</h1>"
                "<p>the &amp; argument</p></body></html>",
            )
            text = ingest.extract_text(path)
            self.assertIn("Real Title", text)
            self.assertIn("the & argument", text)  # entities unescaped
            self.assertNotIn("evil()", text)       # script contents dropped
            self.assertNotIn("color:red", text)

    def test_unsupported_format_is_refused_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(tmp, "data.xlsx", "junk")
            with self.assertRaises(ingest.IngestError) as ctx:
                ingest.extract_text(path)
            self.assertIn("xlsx", str(ctx.exception))

    @unittest.skipIf(shutil.which("pdftotext") is None, "pdftotext not installed")
    def test_pdf_goes_through_pdftotext(self):
        # Build a tiny PDF without adding a dependency.
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "t.pdf"
            body = (
                b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
                b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
                b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]"
                b"/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
                b"4 0 obj<</Length 44>>stream\nBT /F1 12 Tf 20 100 Td (Hello Research) Tj ET\n"
                b"endstream endobj\n"
                b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
                b"trailer<</Root 1 0 R>>\n"
            )
            pdf.write_bytes(body)
            try:
                text = ingest.extract_text(pdf)
            except ingest.IngestError:
                self.skipTest("hand-built PDF not parseable by this pdftotext")
            self.assertIsInstance(text, str)


class DeriveTitleTests(unittest.TestCase):
    def test_uses_the_first_plausible_heading(self):
        text = "# On the Hardness of Exchange\n\nAbstract\n\nWe prove..."
        self.assertEqual(ingest.derive_title(text, "fb"), "On the Hardness of Exchange")

    def test_skips_boilerplate_lines(self):
        text = "arXiv:1234.5678\nAbstract\nA Real Title Line Here\n"
        self.assertEqual(ingest.derive_title(text, "fb"), "A Real Title Line Here")

    def test_falls_back_to_the_filename(self):
        self.assertEqual(ingest.derive_title("tiny\n", "my-paper"), "my-paper")


class IngestDocumentTests(unittest.TestCase):
    def _source(self, tmp, name="paper.md", body="# My Result\n\nWe establish X."):
        path = Path(tmp) / name
        path.write_text(body)
        return path

    def test_stores_extracted_text_under_the_lab(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as tmp:
            lab_dir = Path(tmp) / "lab"
            doc = ingest.ingest_document(conn, ids["lab_id"], self._source(tmp), lab_dir)

            row = conn.execute("SELECT * FROM source_documents WHERE id=?", (doc["id"],)).fetchone()
            self.assertEqual(row["title"], "My Result")
            self.assertEqual(row["origin"], "paper.md")
            self.assertTrue((lab_dir / row["path"]).exists())
            self.assertIn("We establish X.", (lab_dir / row["path"]).read_text())
            self.assertEqual(len(row["sha256"]), 64)
        conn.close()

    def test_re_upload_of_identical_content_converges(self):
        """The same document under a different filename must not be
        ingested twice -- students would read it twice."""
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as tmp:
            lab_dir = Path(tmp) / "lab"
            first = ingest.ingest_document(conn, ids["lab_id"], self._source(tmp, "a.md"), lab_dir)
            second = ingest.ingest_document(conn, ids["lab_id"], self._source(tmp, "b.md"), lab_dir)

            self.assertFalse(first["duplicate"])
            self.assertTrue(second["duplicate"])
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0], 1
            )
        conn.close()

    def test_empty_document_is_refused(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "empty.md"
            path.write_text("   \n\n  ")
            with self.assertRaises(ingest.IngestError):
                ingest.ingest_document(conn, ids["lab_id"], path, Path(tmp) / "lab")
        conn.close()

    def test_one_bad_file_does_not_abandon_the_upload(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as tmp:
            good = self._source(tmp, "good.md")
            bad = Path(tmp) / "bad.xlsx"
            bad.write_text("junk")

            ingested, errors = ingest.ingest_all(
                conn, ids["lab_id"], [good, bad], Path(tmp) / "lab"
            )
            self.assertEqual(len(ingested), 1)
            self.assertEqual(len(errors), 1)
            self.assertIn("xlsx", errors[0])
        conn.close()


class RenderCorpusTests(unittest.TestCase):
    def test_empty_when_nothing_uploaded(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(ingest.render_corpus(conn, ids["lab_id"], Path(tmp)), "")
        conn.close()

    def test_marks_uploads_as_the_founders_unpublished_work(self):
        """The third category: real and buildable-on, but a reviewer
        cannot look it up -- the distinction that stopped internal work
        being cited as published."""
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as tmp:
            lab_dir = Path(tmp) / "lab"
            path = Path(tmp) / "p.md"
            path.write_text("# Founder Result\n\nEstablished here.")
            ingest.ingest_document(conn, ids["lab_id"], path, lab_dir)

            rendered = ingest.render_corpus(conn, ids["lab_id"], lab_dir)
            self.assertIn("Founder Result", rendered)
            self.assertIn("may be unpublished", rendered)
            self.assertIn("never as published literature", rendered)
        conn.close()

    def test_long_documents_are_truncated_not_dropped(self):
        conn = fresh_db()
        ids = seed_lab_with_student(conn)
        with tempfile.TemporaryDirectory() as tmp:
            lab_dir = Path(tmp) / "lab"
            path = Path(tmp) / "long.md"
            path.write_text("# Long Paper\n\n" + ("word " * 5000))
            ingest.ingest_document(conn, ids["lab_id"], path, lab_dir)

            rendered = ingest.render_corpus(conn, ids["lab_id"], lab_dir, per_doc_chars=500)
            self.assertIn("Long Paper", rendered)
            self.assertIn("truncated", rendered)
        conn.close()


if __name__ == "__main__":
    unittest.main()
