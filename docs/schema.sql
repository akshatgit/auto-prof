-- auto-prof schema
-- SQLite holds state, relationships, and status. Prose content (task
-- briefs, papers, review rationales, dissertations) lives on disk under
-- lab/<lab_id>/... ; every `path` column below points at that content.
-- See docs/DESIGN.md for the state machines this schema backs.
--
-- The harness MUST issue `PRAGMA foreign_keys = ON;` on every connection
-- it opens (the daemon process AND the web UI process) -- this pragma is
-- per-connection in SQLite, not persisted in the file, so putting it once
-- at the top of this script only affects the connection that runs the
-- script. See docs/DESIGN.md §5.4.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;  -- lets the web UI (§8) poll without locking out the daemon

-- One row per root problem being worked. Unbounded cardinality by design.
-- `pending_review`: a newly created lab (its root problem is unvetted --
-- see autoprof/lab_review.py) cannot dispatch any work yet. Passing
-- review (2 of 3 strong_accept, same threshold as paper review --
-- docs/DESIGN.md §4) flips this to `active` in the same transaction that
-- auto-enqueues the lab's first professor_decompose job -- this is what
-- "review propagates downstream" means concretely.
CREATE TABLE labs (
    id                    INTEGER PRIMARY KEY,
    professor_id          INTEGER NOT NULL REFERENCES professors(id),
    root_problem          TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (status IN ('pending_review', 'active', 'concluded')),
    -- Mirrors papers.review_round/defenses.review_round -- incremented
    -- each time a lab review is re-requested after a failed round, so
    -- `reviews` rows for lab targets can be validated against the
    -- current round the same way paper/defense reviews already are.
    current_review_round  INTEGER NOT NULL DEFAULT 1 CHECK (current_review_round >= 1),
    -- The human's raw idea as given to `create-prof`, verbatim. The root
    -- problem above is a MODEL's formalization of it and is rewritten on
    -- every failed review round; this column is the fixed point those
    -- rewrites are judged against, so a lab cannot be revised away from
    -- what was actually asked for. NULL for labs predating the column.
    seed_idea             TEXT,
    created_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

-- One row per Professor agent, including ones promoted from a student.
-- parent_student_id is set only for promoted professors; NULL means this
-- professor was created directly (e.g. the very first lab, seeded by
-- `autoprof init`).
CREATE TABLE professors (
    id                  INTEGER PRIMARY KEY,
    lab_id              INTEGER REFERENCES labs(id),
    name                TEXT NOT NULL,
    field               TEXT NOT NULL,
    parent_student_id   INTEGER REFERENCES students(id),
    status              TEXT NOT NULL CHECK (status IN ('active', 'retired')),
    memory_path         TEXT NOT NULL,   -- lab/<lab_id>/professors/<id>/memory.md -- see DESIGN.md §6
    memory_compacted_at TEXT,            -- last time memory.md was compacted
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- The decomposition tree under a lab. parent_task_id NULL = a top-level
-- task produced by the professor's initial decomposition of the root
-- problem -- a lab normally has SEVERAL of these (the root problem itself
-- is `labs.root_problem`, not a task row), not exactly one.
CREATE TABLE tasks (
    id                  INTEGER PRIMARY KEY,
    lab_id              INTEGER NOT NULL REFERENCES labs(id),
    parent_task_id      INTEGER REFERENCES tasks(id),
    title               TEXT NOT NULL,
    brief_path          TEXT NOT NULL,   -- lab/<lab_id>/tasks/<id>/brief.md
    direction           TEXT NOT NULL CHECK (direction IN ('prove', 'disprove', 'open')),
    end_criteria        TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN
                            ('open', 'in_progress', 'pending_prof_review',
                             'completed', 'abandoned')),
    assigned_student_id INTEGER REFERENCES students(id),
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A child task's lab must match its parent's lab -- decomposition never
-- crosses lab boundaries.
CREATE TRIGGER trg_tasks_parent_same_lab
BEFORE INSERT ON tasks
WHEN NEW.parent_task_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'child task must belong to the same lab as its parent')
    WHERE (SELECT lab_id FROM tasks WHERE id = NEW.parent_task_id) != NEW.lab_id;
END;

CREATE TRIGGER trg_tasks_updated_at
AFTER UPDATE ON tasks
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE tasks SET updated_at = datetime('now') WHERE id = NEW.id;
END;

-- One row per Student agent. `students.task_id` is the single writable
-- source of truth for "what is this student assigned to right now";
-- `tasks.assigned_student_id` is a DERIVED back-pointer that application
-- code must never write directly -- only ever write students.task_id and
-- let the triggers below propagate it. (SQLite triggers can't forbid a
-- direct write to a specific column outright, so this is an invariant the
-- harness's data-access layer must honor, not one the schema can refuse
-- on its own -- flagged explicitly rather than implied.)
CREATE TABLE students (
    id                      INTEGER PRIMARY KEY,
    -- UNIQUE (not just indexed): a task can have at most one assigned
    -- student. SQLite allows multiple NULLs through a UNIQUE column, so
    -- unassigned students don't collide with each other -- only two
    -- students both pointing at the SAME non-NULL task_id is rejected.
    -- This is what actually prevents two students racing onto one task
    -- through independent INSERT/UPDATE statements (the sync triggers
    -- below only keep tasks.assigned_student_id in agreement with
    -- whichever write happened last; they can't by themselves stop two
    -- writes from targeting the same task_id in the first place).
    task_id                 INTEGER UNIQUE REFERENCES tasks(id),  -- NULL while unassigned (e.g. after task abandoned)
    professor_id            INTEGER NOT NULL REFERENCES professors(id),
    status                  TEXT NOT NULL CHECK (status IN
                                ('working', 'writing_paper', 'in_review',
                                 'defending', 'graduated', 'stuck',
                                 'unassigned')),
    memory_path             TEXT NOT NULL,   -- lab/<lab_id>/students/<id>/memory.md -- see DESIGN.md §6
    memory_compacted_at     TEXT,
    -- Human pause, orthogonal to `status`: a `working` student paused via
    -- `autoprof student stop` stays `working` (so resuming doesn't
    -- require guessing what state they were in) but job dispatch (once
    -- Phase 4's daemon exists) must skip any student with paused_at NOT
    -- NULL. NULL = not paused. See docs/TASKS.md Phase 3.
    paused_at               TEXT,
    created_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Keep tasks.assigned_student_id in sync on INSERT as well as UPDATE --
-- the original version only covered UPDATE OF task_id, so a student
-- created with a non-NULL task_id left the task's back-pointer stale
-- until the next reassignment.
CREATE TRIGGER trg_students_task_assign_insert
AFTER INSERT ON students
WHEN NEW.task_id IS NOT NULL
BEGIN
    UPDATE tasks SET assigned_student_id = NEW.id WHERE id = NEW.task_id;
END;

CREATE TRIGGER trg_students_task_assign_update
AFTER UPDATE OF task_id ON students
BEGIN
    UPDATE tasks SET assigned_student_id = NULL
        WHERE assigned_student_id = NEW.id AND id != COALESCE(NEW.task_id, -1);
    UPDATE tasks SET assigned_student_id = NEW.id
        WHERE NEW.task_id IS NOT NULL AND id = NEW.task_id;
END;

-- A student's professor and their task's lab-owning professor must match
-- (a student can't be supervised by one professor while working a task
-- that belongs to a different professor's lab). Checked on INSERT and on
-- every subsequent UPDATE that touches either foreign key -- the original
-- version only checked at INSERT, so reassigning task_id or transferring
-- professor_id later could silently create a cross-lab mismatch.
CREATE TRIGGER trg_students_task_same_lab_insert
BEFORE INSERT ON students
WHEN NEW.task_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'student.professor_id must own the lab that task_id belongs to')
    WHERE (SELECT lab_id FROM tasks WHERE id = NEW.task_id)
       != (SELECT lab_id FROM professors WHERE id = NEW.professor_id);
END;

CREATE TRIGGER trg_students_task_same_lab_update
BEFORE UPDATE OF task_id, professor_id ON students
WHEN NEW.task_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'student.professor_id must own the lab that task_id belongs to')
    WHERE (SELECT lab_id FROM tasks WHERE id = NEW.task_id)
       != (SELECT lab_id FROM professors WHERE id = NEW.professor_id);
END;

-- On task abandonment, release whichever student was assigned rather than
-- leaving them pointed at a closed task (see docs/DESIGN.md §3.1).
CREATE TRIGGER trg_tasks_abandon_releases_student
AFTER UPDATE OF status ON tasks
WHEN NEW.status = 'abandoned' AND OLD.status != 'abandoned' AND NEW.assigned_student_id IS NOT NULL
BEGIN
    UPDATE students SET task_id = NULL, status = 'unassigned' WHERE id = NEW.assigned_student_id;
END;

-- Novel-work submissions. A task can yield any number of papers.
-- review_round tracks revise/re-submit cycles after a rejection -- each
-- round gets a fresh set of reviewer verdicts (see `reviews.review_round`
-- below); the paper row itself is reused rather than duplicated so the
-- accepted/rejected history stays attached to one paper identity.
-- The assumption ledger: what the work is standing on.
--
-- First-principles discipline, made checkable. Three failures in one run
-- traced to unexamined inherited premises: a mis-cited reference reached
-- three papers because nobody questioned it; a student contradicted his
-- own lab's accepted results because he took his brief's framing as
-- given; and another ground out rank-by-rank casework when his own lemmas
-- already implied the general theorem.
--
-- Recording assumptions explicitly does three things prose cannot: it
-- separates what was DERIVED from what was INHERITED, it gives the
-- verifier tool a concrete target ("this one is finite -- check it"), and
-- it makes a refuted assumption traceable to everything that leaned on
-- it rather than leaving someone to grep.
CREATE TABLE assumptions (
    id         INTEGER PRIMARY KEY,
    lab_id     INTEGER NOT NULL REFERENCES labs(id),
    task_id    INTEGER REFERENCES tasks(id),
    student_id INTEGER REFERENCES students(id),
    statement  TEXT NOT NULL,
    -- Where it came from. 'inherited' is the dangerous one: taken from a
    -- brief, a root problem or a prior paper without being re-derived.
    source     TEXT NOT NULL CHECK (source IN
                   ('root_problem', 'brief', 'prior_paper', 'derived', 'inherited')),
    -- 'assumed'  -- taken on faith, not yet examined
    -- 'derived'  -- proved from definitions in this work
    -- 'verified' -- checked computationally or against a source
    -- 'refuted'  -- found false; everything depending on it is suspect
    status     TEXT NOT NULL CHECK (status IN ('assumed', 'derived', 'verified', 'refuted')),
    -- How it was settled: a tool_runs id, a reference id, a proof sketch.
    evidence   TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_assumptions_task ON assumptions(task_id, status);


-- Tool runs: computations and figures students produced mechanically.
--
-- Students reason about finite combinatorial claims ("this system has
-- defect 1/2", "the rank-five spectrum is X") with no way to CHECK them,
-- and draw figures freehand. Both are mechanisable, and a checked claim
-- is worth far more at review than an asserted one.
--
-- Every run is recorded rather than being an invisible side effect: a
-- paper that says "verified by exhaustive search" must be traceable to
-- the exact program that was run and the exact output it produced.
-- Available to every lab -- these are lab-agnostic capabilities.
CREATE TABLE tool_runs (
    id         INTEGER PRIMARY KEY,
    lab_id     INTEGER NOT NULL REFERENCES labs(id),
    task_id    INTEGER REFERENCES tasks(id),
    student_id INTEGER REFERENCES students(id),
    tool       TEXT NOT NULL CHECK (tool IN ('verify', 'visualize', 'readfile', 'propose_patch', 'apply_patch', 'fetch')),
    -- The program or chart spec the student supplied, and what came back.
    input_path  TEXT NOT NULL,
    output_path TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN ('ok', 'error', 'timeout')),
    summary    TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_tool_runs_task ON tool_runs(task_id, created_at);


-- Research uploaded by the lab's founder to bootstrap it.
--
-- A lab founded from a one-line idea has nothing concrete to stand on;
-- one founded from the founder's actual papers and notes starts from
-- their established results. These documents are a THIRD category,
-- distinct from both published literature and internal lab results:
-- they are real and their full text is available in the lab, but they
-- may be unpublished, so a reviewer cannot necessarily look them up.
-- Conflating the three is exactly the mistake that made a student cite
-- internal work as though it were published.
CREATE TABLE source_documents (
    id         INTEGER PRIMARY KEY,
    lab_id     INTEGER NOT NULL REFERENCES labs(id),
    title      TEXT NOT NULL,
    -- lab/<lab_id>/sources/<id>-<slug>.txt -- the extracted text, so
    -- students read the same content regardless of the original format.
    path       TEXT NOT NULL,
    -- Original filename, kept so provenance survives the extraction.
    origin     TEXT NOT NULL,
    -- Content hash of the extracted text: detects a re-upload of the same
    -- document under a different name, and pins what students actually read.
    sha256     TEXT NOT NULL,
    word_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (lab_id, sha256)
);


-- Shared reference bank: the lab's collective bibliographic memory.
--
-- Motivated by a real failure: a citation with a fabricated title reached
-- three separate papers before a reviewer looked up the actual record.
-- Students had no authoritative source for references, so each invented
-- plausible-looking ones independently.
--
-- GLOBAL, not lab-scoped. A reference is a fact about the world; scoping
-- it per lab duplicates entries and lets two labs hold different titles
-- for the same work, which is precisely how the bad citation survived.
-- It is also what makes this a shared memory across labs and sessions:
-- a lab's accepted papers enrol here and become citable by later work.
CREATE TABLE reference_works (
    id          INTEGER PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('internal_paper', 'external_work')),
    title       TEXT NOT NULL,
    authors     TEXT NOT NULL,
    venue       TEXT,
    year        INTEGER,
    -- arXiv id, DOI or URL. UNIQUE so the same work cannot be entered
    -- twice under two different titles -- the failure mode above.
    identifier  TEXT UNIQUE,
    -- Papers this lab produced point back at their row; external works
    -- leave it NULL.
    paper_id    INTEGER REFERENCES papers(id),
    -- 'verified' means someone confirmed this work exists AND that title,
    -- authors and venue match the real record. Students may cite verified
    -- entries; anything else must be declared an assumption rather than
    -- presented as a source.
    status      TEXT NOT NULL CHECK (status IN ('unverified', 'verified', 'disputed')),
    verified_at TEXT,
    notes       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_reference_works_status ON reference_works(status);

-- Provenance edges: which paper cited which reference. When a reference
-- is later found wrong, this is what identifies every contaminated paper
-- instead of guessing -- the selective-traceback case from the resilience
-- design, applied to bibliography.
CREATE TABLE reference_citations (
    paper_id     INTEGER NOT NULL REFERENCES papers(id),
    reference_id INTEGER NOT NULL REFERENCES reference_works(id),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (paper_id, reference_id)
);


-- Multi-student collaboration.
--
-- The original model was one task -> one student -> one paper, which has
-- no way to express "these three results are one paper". Combining them
-- is not concatenation: the students must read each other's work, resolve
-- conflicting lemmas, and agree a single narrative -- so it needs its own
-- long-horizon loop, the same shape as supervision.
--
-- A collaboration is ANCHORED TO A TASK rather than replacing it. That
-- keeps every existing invariant intact: students.task_id stays UNIQUE
-- (one student per task, which is what prevents two students racing onto
-- the same work), papers.task_id still points at a real task, and the
-- trg_papers_student_assigned trigger still holds because the paper's
-- papers.student_id is the anchor task's assigned student -- the lead
-- author. Co-authors are carried in paper_authors.
CREATE TABLE collaborations (
    id             INTEGER PRIMARY KEY,
    lab_id         INTEGER NOT NULL REFERENCES labs(id),
    -- The anchor task: where the combined work lives and who leads it.
    task_id        INTEGER NOT NULL UNIQUE REFERENCES tasks(id),
    goal           TEXT NOT NULL,   -- what combining these results is meant to achieve
    status         TEXT NOT NULL CHECK (status IN
                       ('working', 'writing', 'concluded', 'abandoned')),
    round          INTEGER NOT NULL DEFAULT 0 CHECK (round >= 0),
    -- Shared working memory, distinct from any member's own memory.md.
    -- This is the agreed joint state; members' individual memories stay
    -- theirs and are not overwritten by the collaboration.
    memory_path    TEXT NOT NULL,   -- lab/<lab_id>/collaborations/<id>/memory.md
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE collaboration_members (
    collaboration_id INTEGER NOT NULL REFERENCES collaborations(id),
    student_id       INTEGER NOT NULL REFERENCES students(id),
    -- Exactly one lead: the anchor task's assigned student, who is also
    -- papers.student_id. Enforced by trg_collaboration_single_lead.
    role             TEXT NOT NULL CHECK (role IN ('lead', 'co')),
    joined_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (collaboration_id, student_id)
);

CREATE TRIGGER trg_collaboration_single_lead
BEFORE INSERT ON collaboration_members
WHEN NEW.role = 'lead'
BEGIN
    SELECT RAISE(ABORT, 'a collaboration has exactly one lead author')
    WHERE EXISTS (
        SELECT 1 FROM collaboration_members
        WHERE collaboration_id = NEW.collaboration_id AND role = 'lead'
    );
END;

-- One contribution per member per round: what each student brought to
-- that round, kept separately so the synthesis step can see who said what
-- and so a disagreement is attributable rather than silently merged away.
CREATE TABLE collaboration_contributions (
    id               INTEGER PRIMARY KEY,
    collaboration_id INTEGER NOT NULL REFERENCES collaborations(id),
    student_id       INTEGER NOT NULL REFERENCES students(id),
    round            INTEGER NOT NULL CHECK (round >= 1),
    path             TEXT NOT NULL,  -- .../collaborations/<id>/rounds/<round>/<student_id>.md
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (collaboration_id, student_id, round)
);

-- Multi-author papers. papers.student_id remains the lead/corresponding
-- author (and must stay consistent with the anchor task); this table adds
-- every author including that lead, with an explicit order so the byline
-- is deterministic rather than dependent on row order.
CREATE TABLE paper_authors (
    paper_id     INTEGER NOT NULL REFERENCES papers(id),
    student_id   INTEGER NOT NULL REFERENCES students(id),
    author_order INTEGER NOT NULL CHECK (author_order >= 1),
    PRIMARY KEY (paper_id, student_id),
    UNIQUE (paper_id, author_order)
);


-- Structured failure memory (§18 of the resilience design).
--
-- Every terminal failure and every successful recovery writes a row, so
-- the same dead remediation is not retried on the next occurrence and a
-- preventive rule can be surfaced to whoever plans the next attempt.
-- Distinct from `events`, which records what happened: this records what
-- was WRONG, what fixed it, and what should be done differently.
CREATE TABLE failure_memories (
    id                   INTEGER PRIMARY KEY,
    job_id               INTEGER REFERENCES jobs(id),
    classification       TEXT NOT NULL,   -- see autoprof/recovery.py DOMAINS
    symptom              TEXT NOT NULL,   -- the observed error, trimmed
    target_type          TEXT,
    target_id            INTEGER,
    successful_remediation TEXT,          -- NULL if nothing worked
    failed_remediations  TEXT,            -- newline-separated, in order tried
    preventive_rule      TEXT,
    resolved             INTEGER NOT NULL DEFAULT 0 CHECK (resolved IN (0, 1)),
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_failure_memories_class ON failure_memories(classification, created_at);


-- Supervision meetings: the iterative student<->professor loop that runs
-- BEFORE a paper is written (docs/DESIGN.md §3.2 step 1-2).
--
-- Without this the student worked once and wrote up immediately, so the
-- professor first saw the research as a finished paper and the only
-- feedback channel was peer review -- which is far too late and far more
-- expensive. Each row is one meeting: the professor read the student's
-- current memory and either sent them back to work with guidance, agreed
-- it was ready to write up, or abandoned the line of attack.
--
-- Kept as its own table rather than folded into student memory.md because
-- both sides need their own durable record: memory.md is overwritten
-- wholesale by the student each round, and the professor's guidance must
-- survive that to accumulate across a long research horizon.
CREATE TABLE supervisions (
    id            INTEGER PRIMARY KEY,
    task_id       INTEGER NOT NULL REFERENCES tasks(id),
    student_id    INTEGER NOT NULL REFERENCES students(id),
    round         INTEGER NOT NULL CHECK (round >= 1),
    verdict       TEXT NOT NULL CHECK (verdict IN ('continue', 'ready', 'abandon')),
    guidance_path TEXT NOT NULL,   -- lab/<lab_id>/tasks/<task_id>/supervision/<round>.md
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    -- One meeting per round per task: a duplicate would double-advance the
    -- loop and let two contradictory guidances both count as "the latest".
    UNIQUE (task_id, round)
);

CREATE INDEX idx_supervisions_task ON supervisions(task_id, round);


CREATE TABLE papers (
    id              INTEGER PRIMARY KEY,
    task_id         INTEGER NOT NULL REFERENCES tasks(id),
    student_id      INTEGER NOT NULL REFERENCES students(id),
    path            TEXT NOT NULL,   -- lab/<lab_id>/tasks/<id>/papers/<id>/draft.md (overwritten each round)
    title           TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN
                        ('draft', 'in_review', 'accepted', 'rejected')),
    review_round    INTEGER NOT NULL DEFAULT 1 CHECK (review_round >= 1),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A paper's student must actually be assigned to that paper's task at
-- submission time -- otherwise a paper could be attributed to a student
-- working (or supervised in) an unrelated task/lab.
CREATE TRIGGER trg_papers_student_matches_task
BEFORE INSERT ON papers
BEGIN
    SELECT RAISE(ABORT, 'papers.student_id must be assigned to papers.task_id')
    WHERE (SELECT task_id FROM students WHERE id = NEW.student_id) != NEW.task_id;
END;

-- Dissertation submissions. One row per student; review_round tracks
-- revise/re-submit cycles the same way papers.review_round does. The
-- partial unique index below is what actually enforces "one defense at a
-- time" (previously only a comment, not enforced).
CREATE TABLE defenses (
    id              INTEGER PRIMARY KEY,
    student_id      INTEGER NOT NULL REFERENCES students(id),
    path            TEXT NOT NULL,   -- lab/<lab_id>/students/<id>/defense.md (overwritten each round)
    status          TEXT NOT NULL CHECK (status IN
                        ('draft', 'in_review', 'passed', 'failed')),
    review_round    INTEGER NOT NULL DEFAULT 1 CHECK (review_round >= 1),
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Only one non-terminal (draft/in_review) defense per student at a time.
-- A student CAN have multiple 'failed' rows over time (one per exhausted
-- round) if the harness models revision as a new row rather than mutating
-- review_round in place; either convention is fine as long as at most one
-- row per student is ever draft/in_review, which is what this enforces.
CREATE UNIQUE INDEX idx_defenses_one_active_per_student
    ON defenses(student_id) WHERE status IN ('draft', 'in_review');

-- Individual Codex reviewer verdicts. target_type/target_id is a
-- polymorphic reference to either a paper or a defense; reviewer_index is
-- 1..3 for papers, 1..5 for defenses; review_round matches the
-- paper's/defense's review_round at submission time, so a rejected-then-
-- revised paper gets a fresh set of reviewer rows instead of colliding
-- with the UNIQUE constraint below on resubmission.
CREATE TABLE reviews (
    id              INTEGER PRIMARY KEY,
    target_type     TEXT NOT NULL CHECK (target_type IN ('paper', 'defense', 'lab')),
    target_id       INTEGER NOT NULL,
    review_round    INTEGER NOT NULL CHECK (review_round >= 1),
    reviewer_index  INTEGER NOT NULL,
    verdict         TEXT NOT NULL CHECK (verdict IN
                        ('strong_reject', 'reject', 'weak_reject',
                         'weak_accept', 'accept', 'strong_accept')),
    rationale_path  TEXT NOT NULL,   -- .../reviews/<round>/<reviewer_index>.md
    reviewer_backend TEXT,           -- which model family judged (panel audit)
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (target_type, target_id, review_round, reviewer_index)
);

-- Validate target existence, that review_round matches the target's
-- CURRENT review_round (a review can't be filed against a stale round),
-- and reviewer_index bounds (3 reviewers for papers, 5 for defenses --
-- see docs/DESIGN.md §4) at insert time, since target_type/target_id is
-- polymorphic and can't carry a real foreign key.
CREATE TRIGGER trg_reviews_valid_target
BEFORE INSERT ON reviews
BEGIN
    SELECT RAISE(ABORT, 'reviews.target_id does not reference an existing paper')
    WHERE NEW.target_type = 'paper'
      AND NOT EXISTS (SELECT 1 FROM papers WHERE id = NEW.target_id);

    SELECT RAISE(ABORT, 'reviews.target_id does not reference an existing defense')
    WHERE NEW.target_type = 'defense'
      AND NOT EXISTS (SELECT 1 FROM defenses WHERE id = NEW.target_id);

    SELECT RAISE(ABORT, 'reviews.target_id does not reference an existing lab')
    WHERE NEW.target_type = 'lab'
      AND NOT EXISTS (SELECT 1 FROM labs WHERE id = NEW.target_id);

    SELECT RAISE(ABORT, 'review_round does not match the paper''s current review_round')
    WHERE NEW.target_type = 'paper'
      AND NEW.review_round != (SELECT review_round FROM papers WHERE id = NEW.target_id);

    SELECT RAISE(ABORT, 'review_round does not match the defense''s current review_round')
    WHERE NEW.target_type = 'defense'
      AND NEW.review_round != (SELECT review_round FROM defenses WHERE id = NEW.target_id);

    SELECT RAISE(ABORT, 'review_round does not match the lab''s current_review_round')
    WHERE NEW.target_type = 'lab'
      AND NEW.review_round != (SELECT current_review_round FROM labs WHERE id = NEW.target_id);

    SELECT RAISE(ABORT, 'paper reviewer_index must be 1..3')
    WHERE NEW.target_type = 'paper' AND NEW.reviewer_index NOT BETWEEN 1 AND 3;

    SELECT RAISE(ABORT, 'defense reviewer_index must be 1..5')
    WHERE NEW.target_type = 'defense' AND NEW.reviewer_index NOT BETWEEN 1 AND 5;

    SELECT RAISE(ABORT, 'lab reviewer_index must be 1..3')
    WHERE NEW.target_type = 'lab' AND NEW.reviewer_index NOT BETWEEN 1 AND 3;
END;

-- The human growth gate (§3.5/§7). A promoted professor + new lab are
-- only created once a human approves the corresponding row here.
-- `resulting_professor_id`/`resulting_lab_id` are both set together, in
-- the SAME transaction that flips status to 'approved' -- approval is
-- never "just" a status update (see docs/DESIGN.md §3.5 for the exact
-- transaction). The UNIQUE constraint on student_id means a graduating
-- student can only ever have one proposal, so a concurrent double-click
-- on "approve" in the web UI can't create two labs from one graduation.
CREATE TABLE lab_proposals (
    id                      INTEGER PRIMARY KEY,
    student_id              INTEGER NOT NULL UNIQUE REFERENCES students(id),
    proposed_name           TEXT NOT NULL,
    proposed_field          TEXT NOT NULL,
    proposed_problem        TEXT NOT NULL,
    status                  TEXT NOT NULL CHECK (status IN
                                ('pending_approval', 'approved', 'rejected')),
    resulting_professor_id  INTEGER REFERENCES professors(id),  -- set iff approved
    resulting_lab_id        INTEGER REFERENCES labs(id),        -- set iff approved
    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    decided_at              TEXT,
    -- Enforces the "never just a status update" invariant from
    -- docs/DESIGN.md §3.5 at the row level: a row can't be marked
    -- 'approved' without both resulting ids present, and can't have
    -- either resulting id set unless it IS 'approved'. This doesn't by
    -- itself make the professors+labs+lab_proposals writes one atomic
    -- transaction (SQLite CHECK constraints can't see other tables) --
    -- the application must still wrap all three inserts/updates in one
    -- transaction as §3.5 describes; this constraint only rejects the
    -- specific bad state of a partially-completed approval being left
    -- behind if that transaction is done wrong.
    CHECK (
        (status = 'approved' AND resulting_professor_id IS NOT NULL AND resulting_lab_id IS NOT NULL)
        OR (status != 'approved' AND resulting_professor_id IS NULL AND resulting_lab_id IS NULL)
    )
);

-- The resumable work queue. Every unit of dispatched work -- a professor
-- decomposition call, a student work session, a paper draft, a single
-- Codex review -- gets a row here before it runs. See docs/DESIGN.md §5
-- for why this (not "recompute what to do next") is what makes daemon
-- restarts safe, and §5.2 for the lease protocol that makes it safe
-- against a still-running-but-slow subprocess, not just a dead one.
CREATE TABLE jobs (
    id              INTEGER PRIMARY KEY,
    kind            TEXT NOT NULL,   -- e.g. 'professor_decompose', 'student_work',
                                      -- 'paper_review', 'defense_review', 'lab_review',
                                      -- 'professor_callback', 'memory_compact'
    target_type     TEXT NOT NULL,   -- 'task' | 'paper' | 'defense' | 'lab' | 'lab_proposal' | 'professor' | 'student'
    target_id       INTEGER NOT NULL,
    -- 'cancelled' exists so a job is NEVER removed by DELETE. SQLite
    -- reuses a freed rowid, so deleting a job lets a later INSERT take its
    -- id while a daemon may still hold that id in flight -- observed once
    -- as a job whose recorded kind and recorded event disagreed. Cancel by
    -- marking; the row and its id then live forever.
    status          TEXT NOT NULL CHECK (status IN
                        ('pending', 'running', 'done', 'failed', 'cancelled')),

    -- Stable operation identity (§4). jobs.id is a rowid and therefore
    -- reusable; operation_id never is. Side-effecting work and failure
    -- memories reference this, so identity survives any row churn.
    operation_id    TEXT NOT NULL UNIQUE DEFAULT (lower(hex(randomblob(16)))),

    -- Only meaningful for review-kind jobs ('lab_review', and future
    -- 'paper_review'/'defense_review' dispatch) -- which reviewer slot
    -- (1..N) and which round this job's eventual `reviews` row belongs
    -- to. NULL for non-review job kinds. See autoprof/lab_review.py.
    review_round    INTEGER,
    reviewer_index  INTEGER,

    -- Lease protocol (§5.2): claiming a job is an atomic
    -- `UPDATE jobs SET status='running', lease_id=<random>, lease_expires_at=<now+timeout>
    --  WHERE id=? AND status='pending'` -- the runner then only writes
    -- results/marks done if lease_id still matches its own token. A job
    -- whose lease_expires_at has passed is eligible to be reclaimed by
    -- recover_stuck_jobs() regardless of whether the original process is
    -- still alive, and the stale process's eventual write is rejected by
    -- the lease-id check instead of silently double-applying.
    lease_id         TEXT,
    lease_expires_at TEXT,

    -- Execution failures (crashes, malformed output, non-rate-limit
    -- errors) vs. rate-limit waits are counted separately and follow
    -- different policies -- see docs/DESIGN.md §5.1/§5.3. `attempts`
    -- driving retry/backoff must never be conflated with rate-limit
    -- waiting, which is not a failure.
    attempts         INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    max_attempts     INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts >= 1),
    last_error       TEXT,
    rate_limit_count INTEGER NOT NULL DEFAULT 0 CHECK (rate_limit_count >= 0),

    not_before      TEXT,   -- job is ineligible for dispatch until this time; set on
                             -- error backoff (§5.1) or rate-limit backoff (§5.3).
                             -- NULL = eligible immediately.
    wait_reason     TEXT CHECK (wait_reason IN ('rate_limited', 'error_backoff') OR wait_reason IS NULL),
    model_version   TEXT,   -- e.g. 'claude-sonnet-5' / 'codex-...'; set once the job completes.
                             -- Lets a multi-year run correlate output-quality shifts with
                             -- model version changes -- see docs/DESIGN.md §6.5

    -- Backend-side conversation id (Codex `thread_id`), captured from the
    -- first attempt and reused by every later one. A job that dies partway
    -- -- token/usage exhaustion, a crash -- is resumed with `codex exec
    -- resume <id>` on retry instead of restarting from an empty context,
    -- so a long derivation isn't thrown away and re-paid for. NULL until
    -- a backend that supports sessions has run at least once.
    backend_session_id TEXT,

    started_at      TEXT,   -- set when claimed (status -> running)
    completed_at    TEXT,   -- set when status -> done or failed (terminal)

    -- Set when this job was created by `autoprof student replay` rather
    -- than the normal state-machine dispatch -- points at the original
    -- job being re-run. NULL for normally-dispatched jobs. See
    -- docs/TASKS.md Phase 3 / autoprof/student_ctl.py.
    replayed_from_job_id INTEGER REFERENCES jobs(id),

    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TRIGGER trg_jobs_updated_at
AFTER UPDATE ON jobs
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE jobs SET updated_at = datetime('now') WHERE id = NEW.id;
END;

-- Validate jobs.target_id references an existing row of the declared
-- target_type -- the same polymorphic-FK gap existed here as on `reviews`.
CREATE TRIGGER trg_jobs_valid_target
BEFORE INSERT ON jobs
BEGIN
    SELECT RAISE(ABORT, 'jobs.target_id does not reference an existing task')
    WHERE NEW.target_type = 'task' AND NOT EXISTS (SELECT 1 FROM tasks WHERE id = NEW.target_id);

    SELECT RAISE(ABORT, 'jobs.target_id does not reference an existing paper')
    WHERE NEW.target_type = 'paper' AND NOT EXISTS (SELECT 1 FROM papers WHERE id = NEW.target_id);

    SELECT RAISE(ABORT, 'jobs.target_id does not reference an existing defense')
    WHERE NEW.target_type = 'defense' AND NOT EXISTS (SELECT 1 FROM defenses WHERE id = NEW.target_id);

    SELECT RAISE(ABORT, 'jobs.target_id does not reference an existing lab_proposal')
    WHERE NEW.target_type = 'lab_proposal' AND NOT EXISTS (SELECT 1 FROM lab_proposals WHERE id = NEW.target_id);

    SELECT RAISE(ABORT, 'jobs.target_id does not reference an existing professor')
    WHERE NEW.target_type = 'professor' AND NOT EXISTS (SELECT 1 FROM professors WHERE id = NEW.target_id);

    SELECT RAISE(ABORT, 'jobs.target_id does not reference an existing student')
    WHERE NEW.target_type = 'student' AND NOT EXISTS (SELECT 1 FROM students WHERE id = NEW.target_id);
END;

-- Append-only audit/event log. Every job that completes (successfully or
-- not) writes exactly one row here with a pointer to whatever it
-- produced -- this is the source material memory_compact jobs read (see
-- docs/DESIGN.md §6.3), and the thing `professors`/`students` decision
-- rationale traces back to when you ask "why did this happen" years
-- later. Never updated or deleted after insert.
CREATE TABLE events (
    id              INTEGER PRIMARY KEY,
    -- Nullable: most events trace back to a completed job, but 'human'
    -- actor events (manual student edit/stop/resume/replay -- see
    -- docs/TASKS.md Phase 3) are not the output of any job and must still
    -- be auditable. `replay` is the one human action that DOES produce a
    -- job_id -- it creates a new job row -- so that event sets it.
    job_id          INTEGER REFERENCES jobs(id),
    actor_type      TEXT NOT NULL CHECK (actor_type IN ('professor', 'student', 'reviewer', 'daemon', 'human')),
    actor_id        INTEGER,        -- professors.id or students.id; NULL for daemon-internal/human events
    event_type      TEXT NOT NULL,  -- e.g. 'task_decomposed', 'paper_submitted', 'verdict_recorded',
                                     -- 'callback_decided', 'memory_compacted', 'student_stopped',
                                     -- 'student_resumed', 'student_edited', 'job_replayed'
    target_type     TEXT NOT NULL,
    target_id       INTEGER NOT NULL,
    payload_path    TEXT,           -- optional pointer to a file with the full output
    occurred_at     TEXT NOT NULL DEFAULT (datetime('now')),
    CHECK (job_id IS NOT NULL OR actor_type = 'human')
);

-- Provider-scoped (account-level) usage-window state. Distinct from
-- jobs.not_before, which is per-job: a claude/codex subscription's 5-hour
-- or weekly usage cap blocks EVERY call to that provider, not just the
-- one in flight, so it lives here rather than being re-derived per job.
-- See docs/DESIGN.md §5.3.
-- Not CHECK-constrained to a fixed provider list: the /goal directive
-- moved this system onto Codex + Ollama Cloud as the backends (see
-- autoprof/backends/registry.py's BACKEND_CLASSES), and the whole point
-- of the modular backend layer is that adding a new backend later
-- shouldn't require a schema migration here too. `provider` values in
-- practice are whatever Backend.name a registered backend reports.
CREATE TABLE provider_state (
    provider        TEXT PRIMARY KEY,
    blocked_until   TEXT,   -- NULL = not currently blocked
    last_signal     TEXT,   -- raw text of the last rate-limit/window message seen, for debugging
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TRIGGER trg_provider_state_updated_at
AFTER UPDATE ON provider_state
WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE provider_state SET updated_at = datetime('now') WHERE provider = NEW.provider;
END;

CREATE INDEX idx_tasks_lab ON tasks(lab_id);
CREATE INDEX idx_tasks_status ON tasks(status);
CREATE INDEX idx_students_task ON students(task_id);
CREATE INDEX idx_papers_task ON papers(task_id);
CREATE INDEX idx_reviews_target ON reviews(target_type, target_id, review_round);
CREATE INDEX idx_jobs_status ON jobs(status);
CREATE INDEX idx_jobs_not_before ON jobs(not_before);
CREATE INDEX idx_jobs_lease_expires ON jobs(lease_expires_at);
CREATE INDEX idx_lab_proposals_status ON lab_proposals(status);
CREATE INDEX idx_events_job ON events(job_id);
CREATE INDEX idx_events_target ON events(target_type, target_id);
