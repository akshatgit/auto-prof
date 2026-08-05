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
    # Which backend produced this review. Without it there is no way to
    # audit that a panel was actually mixed rather than silently collapsed
    # to one model family -- and no way to measure how often reviewers of
    # DIFFERENT families disagree, which is the signal that says whether
    # the mixing is buying anything.
    ("reviews", "reviewer_backend", "TEXT"),
    # The human's original idea, verbatim, as handed to `create-prof`.
    # It used to be consumed by generate_soul and then dropped, which is
    # what let labs drift: round 1 is anchored to the idea by the soul
    # prompt, but every later round only ever saw the PREVIOUS round's
    # root problem plus reviewer critique. With no memory of what was
    # actually asked for, the revise->review loop is a random walk driven
    # by whatever the reviewers reward -- lab #2 was seeded as a meta lab
    # on auto-prof itself and came out, three rounds later, a
    # preregistered RCT in metascience. NULL for labs created before this
    # column existed; both prompts omit the block when it is missing.
    ("labs", "seed_idea", "TEXT"),
    # Which paper attempt a supervision meeting belongs to. The cap is
    # measured within an attempt, and `round` cannot serve -- it is UNIQUE
    # per task and names the artifact file, so it never resets. Deriving
    # the boundary from created_at was tried and is wrong: those stamps
    # have one-second granularity, so meetings and the paper that ends
    # their attempt can share a timestamp and compare equal. NULL on rows
    # predating the column; they are all treated as attempt 1.
    ("supervisions", "attempt", "INTEGER"),
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
            tool        TEXT NOT NULL,
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
        "review_exchanges",
        """CREATE TABLE review_exchanges (
            id              INTEGER PRIMARY KEY,
            target_type     TEXT NOT NULL CHECK (target_type IN ('paper', 'defense')),
            target_id       INTEGER NOT NULL,
            review_round    INTEGER NOT NULL CHECK (review_round >= 1),
            reviewer_index  INTEGER NOT NULL,
            exchange_round  INTEGER NOT NULL CHECK (exchange_round >= 1),
            request_path    TEXT NOT NULL,
            response_path   TEXT,
            created_at      TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (target_type, target_id, review_round, reviewer_index, exchange_round)
        )""",
    ),
    (
        "idx_review_exchanges_target",
        "CREATE INDEX idx_review_exchanges_target ON review_exchanges"
        "(target_type, target_id, review_round, reviewer_index)",
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
    for spec in _CHECK_REBUILDS:
        _drop_check_constraint(conn, *spec)
    conn.commit()


def _drop_check_constraint(conn, table, marker, ddl, indexes_and_triggers) -> None:
    """Rebuild `table` without a CHECK constraint SQLite cannot alter.

    SQLite cannot alter a CHECK, so the moment a vocabulary in
    docs/schema.sql grows, that file and the deployed table drift apart
    silently -- every test passes, and the drift surfaces only when
    something finally uses a new value, in production. This has now
    happened three times:

      jobs.status    -- 'cancelled' documented, deployed table refuses it
      tasks.direction-- 'implement' documented, deployed table refuses it
      tool_runs.tool -- deployed table frozen at ('verify','visualize'),
                        which every mathematics task satisfied and every
                        implement task violated on its first tool call

    So these vocabularies lose their CHECK and are enforced in code, where
    they can change without touching a database.

    Conditional and idempotent: it runs once, on a table that still has
    the old constraint, and never again. Rewriting a table other tables
    point at is the one genuinely dangerous operation in this file.

    `marker` is the substring identifying the stale constraint, `ddl` the
    replacement CREATE TABLE, and `indexes_and_triggers` the objects the
    drop takes with it.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row is None or marker not in row[0]:
        return

    # PRAGMAs cannot run inside a transaction, and foreign_keys must be off
    # for the drop-and-rename: with it on, dropping `tasks` would cascade
    # or abort against students/papers/supervisions rows pointing at it.
    was_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    # Differential, not absolute: a database can carry a dangling
    # reference from before this ran, and refusing to start over damage
    # the rebuild did not cause would be a worse failure than the one
    # being guarded against.
    broken_before = len(conn.execute("PRAGMA foreign_key_check").fetchall())
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    # Triggers on OTHER tables reference `tasks` -- trg_students_task_assign_insert
    # is one. Modern SQLite re-validates every trigger body during ALTER
    # TABLE RENAME, so the rename below aborts with "no such table:
    # main.tasks" in the window where the old table is dropped and the new
    # one not yet renamed. legacy_alter_table is precisely the documented
    # escape for the 12-step rebuild procedure: it makes RENAME a rename
    # rather than a schema-wide rewrite.
    conn.execute("PRAGMA legacy_alter_table=ON")
    tmp = f"{table}_rebuilt"
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    collist = ", ".join(cols)
    try:
        conn.execute("BEGIN")
        conn.execute(ddl.format(tmp=tmp))
        # Explicit column list, and ids copied verbatim: referencing rows
        # identify their row by id, so renumbering here would silently
        # repoint them at different work.
        conn.execute(f"INSERT INTO {tmp} ({collist}) SELECT {collist} FROM {table}")
        before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        after = conn.execute(f"SELECT COUNT(*) FROM {tmp}").fetchone()[0]
        if before != after:
            raise RuntimeError(f"{table} rebuild lost rows: {before} -> {after}")

        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")
        # Dropping the table dropped its indexes and triggers with it.
        for stmt in indexes_and_triggers:
            conn.execute(stmt)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute("PRAGMA legacy_alter_table=OFF")
        conn.execute(f"PRAGMA foreign_keys={'ON' if was_on else 'OFF'}")

    broken = conn.execute("PRAGMA foreign_key_check").fetchall()
    if len(broken) > broken_before:
        raise RuntimeError(
            f"{table} rebuild broke foreign keys: {broken_before} violations before, "
            f"{len(broken)} after"
        )


# Recreated after the rebuild drops `tasks`. Copies of the corresponding
# blocks in docs/schema.sql.
_TOOL_RUNS_INDEXES = (
    "CREATE INDEX idx_tool_runs_task ON tool_runs(task_id, created_at)",
)

_TASKS_INDEXES_AND_TRIGGERS = (
    "CREATE INDEX idx_tasks_lab ON tasks(lab_id)",
    "CREATE INDEX idx_tasks_status ON tasks(status)",
    """CREATE TRIGGER trg_tasks_parent_same_lab
    BEFORE INSERT ON tasks
    WHEN NEW.parent_task_id IS NOT NULL
    BEGIN
        SELECT RAISE(ABORT, 'child task must belong to the same lab as its parent')
        WHERE (SELECT lab_id FROM tasks WHERE id = NEW.parent_task_id) != NEW.lab_id;
    END""",
    """CREATE TRIGGER trg_tasks_updated_at
    AFTER UPDATE ON tasks
    WHEN NEW.updated_at = OLD.updated_at
    BEGIN
        UPDATE tasks SET updated_at = datetime('now') WHERE id = NEW.id;
    END""",
    """CREATE TRIGGER trg_tasks_abandon_releases_student
    AFTER UPDATE OF status ON tasks
    WHEN NEW.status = 'abandoned' AND OLD.status != 'abandoned'
         AND NEW.assigned_student_id IS NOT NULL
    BEGIN
        UPDATE students SET task_id = NULL, status = 'unassigned'
        WHERE id = NEW.assigned_student_id;
    END""",
)


_TASKS_DDL = """CREATE TABLE {tmp} (
    id                  INTEGER PRIMARY KEY,
    lab_id              INTEGER NOT NULL REFERENCES labs(id),
    parent_task_id      INTEGER REFERENCES tasks(id),
    title               TEXT NOT NULL,
    brief_path          TEXT NOT NULL,
    direction           TEXT NOT NULL,
    end_criteria        TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN
                            ('open', 'in_progress', 'pending_prof_review',
                             'completed', 'abandoned')),
    assigned_student_id INTEGER REFERENCES students(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
)"""

_TOOL_RUNS_DDL = """CREATE TABLE {tmp} (
    id          INTEGER PRIMARY KEY,
    lab_id      INTEGER NOT NULL REFERENCES labs(id),
    task_id     INTEGER REFERENCES tasks(id),
    student_id  INTEGER REFERENCES students(id),
    tool        TEXT NOT NULL,
    input_path  TEXT NOT NULL,
    output_path TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('ok', 'error', 'timeout')),
    summary     TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
)"""

# (table, substring identifying the stale CHECK, replacement DDL, objects
# the DROP takes with it). Each entry runs once, on a database that still
# carries the constraint.
_CHECK_REBUILDS = (
    ("tasks", "CHECK (direction IN", _TASKS_DDL, _TASKS_INDEXES_AND_TRIGGERS),
    ("tool_runs", "CHECK (tool IN", _TOOL_RUNS_DDL, _TOOL_RUNS_INDEXES),
)


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
