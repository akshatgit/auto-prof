"""Ingest the founder's own research to bootstrap a lab.

`create-prof` distils a lab from a sentence, which is fine for an idea you
have not written down yet and useless if you already have papers. This
reads those papers and grounds the professor's root problem in what you
have actually established, rather than in what a model imagines the field
to be.

Uploaded documents are a THIRD reference category, kept distinct from
published literature and from internal lab results. They are real and
their full text is in the lab, but they may be unpublished -- so a student
may read and build on them freely while a reviewer cannot necessarily look
them up. Collapsing that distinction is precisely what made a student cite
internal work as though it were published and get the paper rejected.
"""

import hashlib
import html as html_module
import re
import shutil
import subprocess
from pathlib import Path

TEXT_SUFFIXES = {".md", ".txt", ".rst", ".tex", ".org"}
HTML_SUFFIXES = {".html", ".htm"}
PDF_SUFFIXES = {".pdf"}
SUPPORTED = TEXT_SUFFIXES | HTML_SUFFIXES | PDF_SUFFIXES

_TAG_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\n{3,}")


class IngestError(RuntimeError):
    pass


def extract_text(path: Path) -> str:
    """Pull readable text out of a source document.

    HTML is stripped rather than parsed: these are research papers, and
    what matters is the prose and the mathematics, not the markup. PDFs go
    through `pdftotext` so no Python PDF dependency is added -- the project
    is stdlib-only by design.
    """
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix in TEXT_SUFFIXES:
        return path.read_text(errors="replace")

    if suffix in HTML_SUFFIXES:
        raw = path.read_text(errors="replace")
        raw = _TAG_RE.sub(" ", raw)
        raw = _ANY_TAG_RE.sub(" ", raw)
        return _WS_RE.sub("\n\n", html_module.unescape(raw))

    if suffix in PDF_SUFFIXES:
        if shutil.which("pdftotext") is None:
            raise IngestError(
                f"{path.name}: PDF ingestion needs `pdftotext` (poppler-utils) on PATH. "
                "Convert it to text or Markdown and upload that instead."
            )
        try:
            proc = subprocess.run(
                ["pdftotext", "-layout", str(path), "-"],
                capture_output=True,
                text=True,
                timeout=120,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as e:
            raise IngestError(f"{path.name}: pdftotext timed out") from e
        if proc.returncode != 0:
            raise IngestError(f"{path.name}: pdftotext failed: {(proc.stderr or '').strip()[:200]}")
        return proc.stdout

    raise IngestError(
        f"{path.name}: unsupported format {suffix or '(none)'}. "
        f"Supported: {', '.join(sorted(SUPPORTED))}"
    )


def derive_title(text: str, fallback: str) -> str:
    """First plausible title line, else the filename.

    Deliberately simple: a wrong title is a cosmetic problem the founder
    can correct, whereas refusing to ingest a document because its title
    could not be parsed would be an obstruction.
    """
    for line in text.splitlines():
        stripped = line.strip().lstrip("#").strip()
        if 8 <= len(stripped) <= 200 and not stripped.lower().startswith(
            ("abstract", "introduction", "arxiv:", "doi:", "copyright")
        ):
            return stripped
    return fallback


def _slug(value: str, limit: int = 50) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-")[:limit].lower() or "document"


def ingest_document(conn, lab_id: int, source: Path, lab_dir: Path) -> dict:
    """Extract, store and register one document.

    Returns a dict describing the result, including `duplicate` when the
    same content was already ingested for this lab -- re-uploading a file
    under a new name should converge, not create a second copy that
    students would read twice.
    """
    source = Path(source)
    if not source.is_file():
        raise IngestError(f"{source}: not a file")

    text = extract_text(source).strip()
    if not text:
        raise IngestError(f"{source.name}: no readable text extracted")

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    existing = conn.execute(
        "SELECT * FROM source_documents WHERE lab_id = ? AND sha256 = ?", (lab_id, digest)
    ).fetchone()
    if existing:
        return {"id": existing["id"], "title": existing["title"], "duplicate": True,
                "word_count": existing["word_count"]}

    title = derive_title(text, source.stem)
    word_count = len(text.split())

    cur = conn.execute(
        "INSERT INTO source_documents (lab_id, title, path, origin, sha256, word_count) "
        "VALUES (?, ?, 'pending', ?, ?, ?)",
        (lab_id, title, source.name, digest, word_count),
    )
    doc_id = cur.lastrowid
    relpath = f"{lab_id}/sources/{doc_id}-{_slug(title)}.txt"
    conn.execute("UPDATE source_documents SET path = ? WHERE id = ?", (relpath, doc_id))

    target = Path(lab_dir) / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    conn.commit()

    return {"id": doc_id, "title": title, "duplicate": False, "word_count": word_count}


def ingest_all(conn, lab_id: int, sources, lab_dir: Path) -> tuple[list, list]:
    """Ingest many documents. Returns (ingested, errors).

    One unreadable file must not abandon the whole upload -- the founder
    gets the documents that worked plus a precise list of what didn't.
    """
    ingested, errors = [], []
    for source in sources:
        try:
            ingested.append(ingest_document(conn, lab_id, Path(source), lab_dir))
        except IngestError as e:
            errors.append(str(e))
    return ingested, errors


def render_corpus(conn, lab_id: int, lab_dir: Path, per_doc_chars: int = 6000) -> str:
    """The uploaded corpus as an agent sees it.

    Truncated per document rather than dropped: a professor decomposing
    the root problem needs the shape of every source, not all of one.
    Students who need the full text can read the path, which is given.
    """
    rows = conn.execute(
        "SELECT * FROM source_documents WHERE lab_id = ? ORDER BY id", (lab_id,)
    ).fetchall()
    if not rows:
        return ""

    parts = []
    for row in rows:
        path = Path(lab_dir) / row["path"]
        body = path.read_text(errors="replace") if path.exists() else "(source text missing)"
        truncated = len(body) > per_doc_chars
        parts.append(
            f"--- Source document {row['id']}: {row['title']} "
            f"({row['word_count']} words, from {row['origin']}; full text at {row['path']}) ---\n"
            + body[:per_doc_chars]
            + ("\n[... truncated; read the full text at the path above ...]" if truncated else "")
        )

    return (
        "The founder's own research, uploaded to bootstrap this lab. This is REAL work and you "
        "should build on it directly -- but it may be unpublished, so a reviewer cannot "
        "necessarily look it up. Cite it as the founder's source material, never as published "
        "literature, and do not claim priority over the wider field on its basis alone.\n"
        "<uploaded_research>\n" + "\n\n".join(parts) + "\n</uploaded_research>"
    )
