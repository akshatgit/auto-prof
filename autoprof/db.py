"""SQLite connection helper.

PRAGMA foreign_keys=ON is per-connection in SQLite, not a database-file
setting -- every entry point that opens autoprof.db must call connect()
here (or replicate this) rather than using sqlite3.connect() directly.
See docs/DESIGN.md §5.4.
"""

import sqlite3
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = REPO_ROOT / "autoprof.db"
SCHEMA_PATH = REPO_ROOT / "docs" / "schema.sql"
LAB_DIR = REPO_ROOT / "lab"


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def ensure_initialized(conn: sqlite3.Connection) -> None:
    """Apply docs/schema.sql if the DB is empty. Safe to call every run."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='labs'"
    ).fetchone()
    if row is not None:
        return
    conn.executescript(SCHEMA_PATH.read_text())
