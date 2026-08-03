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
    # With concurrent workers, SQLite serialises writes; without a busy
    # timeout a writer that finds the lock held fails instantly with
    # "database is locked" rather than waiting the few milliseconds a
    # write actually takes. Reads are unaffected (WAL).
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.row_factory = sqlite3.Row
    return conn


# Columns added to an existing table after the schema first shipped.
# schema.sql is the source of truth for a fresh DB; this list is what
# brings an already-populated one up to date. Each entry must be a plain
# nullable ADD COLUMN -- anything needing a backfill or a table rebuild
# does not belong here.
_ADDITIVE_MIGRATIONS = (
    ("jobs", "backend_session_id", "TEXT"),
    # Stable operation identity (§4). Cannot carry the UNIQUE constraint
    # here -- SQLite's ADD COLUMN rejects UNIQUE -- so existing rows are
    # backfilled below and the uniqueness is enforced by a separate index.
    ("jobs", "operation_id", "TEXT"),
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
    (
        "assumptions",
        """CREATE TABLE assumptions (
            id         INTEGER PRIMARY KEY,
            lab_id     INTEGER NOT NULL REFERENCES labs(id),
            task_id    INTEGER REFERENCES tasks(id),
            student_id INTEGER REFERENCES students(id),
            statement  TEXT NOT NULL,
            source     TEXT NOT NULL CHECK (source IN
                           ('root_problem', 'brief', 'prior_paper', 'derived', 'inherited')),
            status     TEXT NOT NULL CHECK (status IN
                           ('assumed', 'derived', 'verified', 'refuted')),
            evidence   TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "idx_assumptions_task",
        "CREATE INDEX idx_assumptions_task ON assumptions(task_id, status)",
    ),
    (
        "tool_runs",
        """CREATE TABLE tool_runs (
            id          INTEGER PRIMARY KEY,
            lab_id      INTEGER NOT NULL REFERENCES labs(id),
            task_id     INTEGER REFERENCES tasks(id),
            student_id  INTEGER REFERENCES students(id),
            tool        TEXT NOT NULL CHECK (tool IN ('verify', 'visualize', 'readfile', 'propose_patch', 'apply_patch', 'fetch')),
            input_path  TEXT NOT NULL,
            output_path TEXT NOT NULL,
            status      TEXT NOT NULL CHECK (status IN ('ok', 'error', 'timeout')),
            summary     TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "idx_tool_runs_task",
        "CREATE INDEX idx_tool_runs_task ON tool_runs(task_id, created_at)",
    ),
    (
        "source_documents",
        """CREATE TABLE source_documents (
            id         INTEGER PRIMARY KEY,
            lab_id     INTEGER NOT NULL REFERENCES labs(id),
            title      TEXT NOT NULL,
            path       TEXT NOT NULL,
            origin     TEXT NOT NULL,
            sha256     TEXT NOT NULL,
            word_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (lab_id, sha256)
        )""",
    ),
    (
        "reference_works",
        """CREATE TABLE reference_works (
            id          INTEGER PRIMARY KEY,
            kind        TEXT NOT NULL CHECK (kind IN ('internal_paper', 'external_work')),
            title       TEXT NOT NULL,
            authors     TEXT NOT NULL,
            venue       TEXT,
            year        INTEGER,
            identifier  TEXT UNIQUE,
            paper_id    INTEGER REFERENCES papers(id),
            status      TEXT NOT NULL CHECK (status IN ('unverified', 'verified', 'disputed')),
            verified_at TEXT,
            notes       TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "idx_reference_works_status",
        "CREATE INDEX idx_reference_works_status ON reference_works(status)",
    ),
    (
        "reference_citations",
        """CREATE TABLE reference_citations (
            paper_id     INTEGER NOT NULL REFERENCES papers(id),
            reference_id INTEGER NOT NULL REFERENCES reference_works(id),
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (paper_id, reference_id)
        )""",
    ),
    (
        "collaborations",
        """CREATE TABLE collaborations (
            id          INTEGER PRIMARY KEY,
            lab_id      INTEGER NOT NULL REFERENCES labs(id),
            task_id     INTEGER NOT NULL UNIQUE REFERENCES tasks(id),
            goal        TEXT NOT NULL,
            status      TEXT NOT NULL CHECK (status IN
                            ('working', 'writing', 'concluded', 'abandoned')),
            round       INTEGER NOT NULL DEFAULT 0 CHECK (round >= 0),
            memory_path TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "collaboration_members",
        """CREATE TABLE collaboration_members (
            collaboration_id INTEGER NOT NULL REFERENCES collaborations(id),
            student_id       INTEGER NOT NULL REFERENCES students(id),
            role             TEXT NOT NULL CHECK (role IN ('lead', 'co')),
            joined_at        TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (collaboration_id, student_id)
        )""",
    ),
    (
        "trg_collaboration_single_lead",
        """CREATE TRIGGER trg_collaboration_single_lead
        BEFORE INSERT ON collaboration_members
        WHEN NEW.role = 'lead'
        BEGIN
            SELECT RAISE(ABORT, 'a collaboration has exactly one lead author')
            WHERE EXISTS (
                SELECT 1 FROM collaboration_members
                WHERE collaboration_id = NEW.collaboration_id AND role = 'lead'
            );
        END""",
    ),
    (
        "collaboration_contributions",
        """CREATE TABLE collaboration_contributions (
            id               INTEGER PRIMARY KEY,
            collaboration_id INTEGER NOT NULL REFERENCES collaborations(id),
            student_id       INTEGER NOT NULL REFERENCES students(id),
            round            INTEGER NOT NULL CHECK (round >= 1),
            path             TEXT NOT NULL,
            created_at       TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (collaboration_id, student_id, round)
        )""",
    ),
    (
        "paper_authors",
        """CREATE TABLE paper_authors (
            paper_id     INTEGER NOT NULL REFERENCES papers(id),
            student_id   INTEGER NOT NULL REFERENCES students(id),
            author_order INTEGER NOT NULL CHECK (author_order >= 1),
            PRIMARY KEY (paper_id, student_id),
            UNIQUE (paper_id, author_order)
        )""",
    ),
    (
        "failure_memories",
        """CREATE TABLE failure_memories (
            id                     INTEGER PRIMARY KEY,
            job_id                 INTEGER REFERENCES jobs(id),
            classification         TEXT NOT NULL,
            symptom                TEXT NOT NULL,
            target_type            TEXT,
            target_id              INTEGER,
            successful_remediation TEXT,
            failed_remediations    TEXT,
            preventive_rule        TEXT,
            resolved               INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
            created_at             TEXT NOT NULL DEFAULT (datetime('now'))
        )""",
    ),
    (
        "idx_failure_memories_class",
        "CREATE INDEX idx_failure_memories_class ON failure_memories(classification, created_at)",
    ),
    (
        "idx_jobs_operation_id",
        "CREATE UNIQUE INDEX idx_jobs_operation_id ON jobs(operation_id)",
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

    # Backfill operation_id before its unique index is created, or the
    # index build fails on pre-existing rows sharing a NULL-free default.
    if conn.execute("SELECT COUNT(*) FROM jobs WHERE operation_id IS NULL").fetchone()[0]:
        conn.execute(
            "UPDATE jobs SET operation_id = lower(hex(randomblob(16))) WHERE operation_id IS NULL"
        )

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
