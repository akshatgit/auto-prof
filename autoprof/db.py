"""SQLite connection helper.

PRAGMA foreign_keys=ON is per-connection in SQLite, not a database-file
setting -- every entry point that opens autoprof.db must call connect()
here (or replicate this) rather than using sqlite3.connect() directly.
See docs/DESIGN.md §5.4.
"""

import re
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


# Columns added to an existing table after the schema first shipped.
# schema.sql is the source of truth for a fresh DB; this list is what
# brings an already-populated one up to date. Each entry must be a plain
# nullable ADD COLUMN -- anything needing a backfill or a table rebuild
# does not belong here.
_ADDITIVE_MIGRATIONS = (
    ("jobs", "backend_session_id", "TEXT"),
)

# Tables added after the schema first shipped. Mirrors the corresponding
# block in docs/schema.sql -- see _apply_missing_tables for why this is a
# literal list rather than parsed from that file.
_ADDITIVE_TABLES = (
    (
        "supervisions",
        """CREATE TABLE supervisions (
            id            INTEGER PRIMARY KEY,
            task_id       INTEGER NOT NULL REFERENCES tasks(id),
            student_id    INTEGER NOT NULL REFERENCES students(id),
            round         INTEGER NOT NULL CHECK (round >= 1),
            verdict       TEXT NOT NULL CHECK (verdict IN ('continue', 'ready', 'abandon')),
            guidance_path TEXT NOT NULL,
            created_at    TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (task_id, round)
        )""",
    ),
    (
        "idx_supervisions_task",
        "CREATE INDEX idx_supervisions_task ON supervisions(task_id, round)",
    ),
)


def ensure_initialized(conn: sqlite3.Connection) -> None:
    """Apply docs/schema.sql if the DB is empty, then apply any additive
    migrations. Safe to call every run."""
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='labs'"
    ).fetchone()
    if row is None:
        conn.executescript(SCHEMA_PATH.read_text())
        return

    # An existing DB predates whatever columns were added since. SQLite has
    # no "ADD COLUMN IF NOT EXISTS", so check the table's own column list --
    # cheaper and more honest than catching OperationalError on a string
    # match of the error message.
    for table, column, decl in _ADDITIVE_MIGRATIONS:
        existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    _apply_missing_tables(conn)
    conn.commit()


def _apply_missing_tables(conn: sqlite3.Connection) -> None:
    """Create tables added to schema.sql after this DB was initialized.

    The DDL is listed explicitly rather than parsed out of schema.sql:
    splitting that file on ';' breaks on trigger bodies, which contain
    their own statements. Each entry must stay a copy of the corresponding
    block in schema.sql -- that file remains the source of truth for a
    fresh DB, this list only catches up an existing one.
    """
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    for name, ddl in _ADDITIVE_TABLES:
        if name not in existing:
            conn.execute(ddl)
