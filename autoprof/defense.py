"""Defense, graduation, and the proposal of a new lab -- §3.4/§3.5.

The end of a student's life in the lab. A nominated student compiles their
accepted papers into one dissertation, five independent reviewers judge it
at 4-of-5 `strong_accept` -- a deliberately harder bar than a paper's
2-of-3, because passing promotes them to lead a lab of their own -- and a
pass produces a `lab_proposals` row for a human to approve.

The bar is higher for a reason worth stating: a paper that slips through
costs one wrong paper, while a defense that slips through founds a lab
that generates wrong papers for years.

Graduation deliberately stops at a PROPOSAL rather than creating the lab.
§3.5 requires the professors/labs/lab_proposals writes to be one
transaction and the decision to be a human's; auto-founding labs on a
5-reviewer vote is how a system quietly turns into an unbounded number of
labs nobody chose to start.
"""

import re
import sqlite3
import uuid
from pathlib import Path

from . import db, jobs
from .artifacts import write_artifact
from .backends.base import Backend
from .events import record_job_event
from .jsonio import extract_json_object

REVIEWER_COUNT = 5
STRONG_ACCEPT_THRESHOLD = 4

_TEMPLATE_PATH = db.REPO_ROOT / "templates" / "defense_template.md"
_RUBRIC_PATH = db.REPO_ROOT / "templates" / "review_rubric.md"
_VERDICT_RE = re.compile(r"^VERDICT:\s*(\w+)\s*$", re.MULTILINE)
_LEADING_HTML_COMMENT_RE = re.compile(r"^\s*<!--.*?-->\s*\n", re.DOTALL)

DOCUMENT_TYPE = "a PhD dissertation submitted for defense"

DEFENSE_PROMPT_TEMPLATE = """You are a PhD student compiling your dissertation. You have been \
nominated for defense on the strength of your accepted work.

Your lab's root problem:
<root_problem>
{root_problem}
</root_problem>

Your accepted papers, in full. The dissertation must synthesise these, not staple them together:
<papers>
{papers}
</papers>

{template}

Write the dissertation following the template above. What matters most:

- It must read as ONE argument. Restate each result in the dissertation's own notation and \
framing; do not paste papers in verbatim.
- The cross-cutting chapter must establish something that does NOT follow from any single paper \
alone. If it merely summarises them, the defense fails -- five reviewers check exactly this.
- Every claim you attribute to one of your papers must be what that paper actually established. \
Do not strengthen a result in the retelling or drop an assumption its proof required.
- Be honest in Limitations about what the body of work does not settle.

Respond with ONLY the dissertation in Markdown. No commentary before or after.
"""

PROPOSAL_PROMPT_TEMPLATE = """You are {name}, a professor. Your student has just passed their \
defense and will now lead a lab of their own. Propose that lab.

Your lab's root problem, which their work came out of:
<root_problem>
{root_problem}
</root_problem>

Their dissertation:
<dissertation>
{dissertation}
</dissertation>

Propose the new professor identity and the root problem their lab will pursue. It must:
- Grow out of what they actually established -- the open questions their work leaves, not a \
restatement of yours.
- Be a genuinely different question from this lab's, not a rescoping of it. Two labs working the \
same problem is waste.
- Meet the same bar a founding root problem does: precise enough to judge a task resolved \
against, neither a single task in disguise nor unclosable.

Respond with ONLY a JSON object, no fences, no commentary:
{{"name": "...", "field": "...", "root_problem": "..."}}
"""


class DefenseError(RuntimeError):
    pass


def request_defense(conn: sqlite3.Connection, student_id: int) -> int | None:
    """Queue the dissertation write-up for a nominated student."""
    student = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if student is None or student["status"] != "defending":
        return None
    live = conn.execute(
        "SELECT COUNT(*) AS n FROM defenses WHERE student_id = ? "
        "AND status IN ('draft', 'in_review')",
        (student_id,),
    ).fetchone()["n"]
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE kind='student_write_defense' AND target_id=? "
        "AND status IN ('pending', 'running')",
        (student_id,),
    ).fetchone()["n"]
    if live or pending:
        return None
    cur = conn.execute(
        "INSERT INTO jobs (kind, target_type, target_id, status) "
        "VALUES ('student_write_defense', 'student', ?, 'pending')",
        (student_id,),
    )
    conn.commit()
    return cur.lastrowid


def _accepted_papers(conn, student_id: int, lab_dir: Path) -> str:
    rows = conn.execute(
        "SELECT DISTINCT papers.* FROM papers "
        "LEFT JOIN paper_authors ON paper_authors.paper_id = papers.id "
        "WHERE papers.status = 'accepted' AND (papers.student_id = ? OR paper_authors.student_id = ?) "
        "ORDER BY papers.id",
        (student_id, student_id),
    ).fetchall()
    if not rows:
        return "(no accepted papers)"
    parts = []
    for paper in rows:
        path = lab_dir / paper["path"]
        body = path.read_text(errors="replace") if path.exists() else "(paper file missing)"
        parts.append(f"--- Paper {paper['id']}: {paper['title']} ---\n{body[:18000]}")
    return "\n\n".join(parts)


def execute_student_write_defense_job(
    conn: sqlite3.Connection, job_id: int, backend: Backend, lab_dir: Path
) -> str:
    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=3600):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    student = conn.execute("SELECT * FROM students WHERE id = ?", (row["target_id"],)).fetchone()
    if student is None:
        return jobs.fail_job(conn, job_id, lease_id, f"no student with id={row['target_id']}")

    professor = conn.execute(
        "SELECT * FROM professors WHERE id = ?", (student["professor_id"],)
    ).fetchone()
    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (professor["lab_id"],)).fetchone()

    live = conn.execute(
        "SELECT COUNT(*) AS n FROM defenses WHERE student_id = ? AND status IN ('draft','in_review')",
        (student["id"],),
    ).fetchone()["n"]
    if live:
        jobs.complete_job(conn, job_id, lease_id)
        return "done"

    template = _LEADING_HTML_COMMENT_RE.sub("", _TEMPLATE_PATH.read_text(), count=1)
    result = jobs.run_with_session(
        conn,
        job_id,
        backend,
        DEFENSE_PROMPT_TEMPLATE.format(
            root_problem=lab["root_problem"],
            papers=_accepted_papers(conn, student["id"], lab_dir),
            template=f"<template>\n{template}\n</template>",
        ),
    )

    if result.rate_limited:
        jobs.record_rate_limit(conn, job_id, lease_id, result.retry_after_seconds)
        return "rate_limited"
    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)
    if len(result.text.split()) < 500:
        return jobs.fail_job(
            conn, job_id, lease_id,
            f"dissertation too short to be substantive ({len(result.text.split())} words)",
        )

    cur = conn.execute(
        "INSERT INTO defenses (student_id, path, status, review_round) "
        "VALUES (?, 'pending', 'in_review', 1)",
        (student["id"],),
    )
    defense_id = cur.lastrowid
    relpath = f"{lab['id']}/students/{student['id']}/defense.md"
    conn.execute("UPDATE defenses SET path = ? WHERE id = ?", (relpath, defense_id))
    write_artifact(lab_dir / relpath, result.text)

    request_defense_review(conn, defense_id)

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"
    record_job_event(
        conn, job_id=job_id, actor_type="student", actor_id=student["id"],
        event_type="defense_submitted", target_type="defense", target_id=defense_id,
        payload_path=relpath,
    )
    conn.commit()
    return "done"


def request_defense_review(conn: sqlite3.Connection, defense_id: int) -> list[int]:
    """Five independent reviewers for the defense's current round."""
    defense = conn.execute("SELECT * FROM defenses WHERE id = ?", (defense_id,)).fetchone()
    if defense is None:
        raise DefenseError(f"no defense with id={defense_id}")
    round_ = defense["review_round"]
    existing = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE kind='defense_review' AND target_id=? "
        "AND review_round=?",
        (defense_id, round_),
    ).fetchone()["n"]
    if existing:
        raise DefenseError(f"defense {defense_id} round {round_} already requested")

    job_ids = []
    for reviewer_index in range(1, REVIEWER_COUNT + 1):
        cur = conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status, review_round, reviewer_index) "
            "VALUES ('defense_review', 'defense', ?, 'pending', ?, ?)",
            (defense_id, round_, reviewer_index),
        )
        job_ids.append(cur.lastrowid)
    conn.commit()
    return job_ids


def build_review_prompt(document: str) -> str:
    """The same rubric papers are held to -- replace(), not format(), since
    the document is full of braces."""
    template = _LEADING_HTML_COMMENT_RE.sub("", _RUBRIC_PATH.read_text(), count=1)
    return template.replace("{DOCUMENT_TYPE}", DOCUMENT_TYPE).replace(
        "{DOCUMENT_CONTENT}", document
    )


def execute_defense_review_job(
    conn: sqlite3.Connection, job_id: int, backend: Backend, lab_dir: Path
) -> str:
    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=3600):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    defense = conn.execute("SELECT * FROM defenses WHERE id = ?", (row["target_id"],)).fetchone()
    if defense is None:
        return jobs.fail_job(conn, job_id, lease_id, f"defense {row['target_id']} no longer exists")
    if row["review_round"] != defense["review_round"]:
        return jobs.fail_job(
            conn, job_id, lease_id,
            f"job is for round {row['review_round']} but defense is on {defense['review_round']}",
        )

    path = lab_dir / defense["path"]
    if not path.exists():
        return jobs.fail_job(conn, job_id, lease_id, f"dissertation missing: {defense['path']}")

    result = jobs.run_with_session(conn, job_id, backend, build_review_prompt(path.read_text()))
    if result.rate_limited:
        jobs.record_rate_limit(conn, job_id, lease_id, result.retry_after_seconds)
        return "rate_limited"
    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)

    matches = _VERDICT_RE.findall(result.text)
    if not matches:
        return jobs.fail_job(
            conn, job_id, lease_id, f"no VERDICT line in defense review: {result.text[:300]}"
        )

    student = conn.execute(
        "SELECT * FROM students WHERE id = ?", (defense["student_id"],)
    ).fetchone()
    professor = conn.execute(
        "SELECT lab_id FROM professors WHERE id = ?", (student["professor_id"],)
    ).fetchone()
    relpath = (
        f"{professor['lab_id']}/students/{student['id']}/defense_reviews/"
        f"{row['review_round']}/{row['reviewer_index']}.md"
    )
    write_artifact(lab_dir / relpath, result.text)
    conn.execute(
        "INSERT INTO reviews (target_type, target_id, review_round, reviewer_index, verdict, rationale_path) "
        "VALUES ('defense', ?, ?, ?, ?, ?)",
        (defense["id"], row["review_round"], row["reviewer_index"], matches[-1], relpath),
    )

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"
    record_job_event(
        conn, job_id=job_id, actor_type="reviewer", actor_id=None,
        event_type="defense_verdict_recorded", target_type="defense", target_id=defense["id"],
        payload_path=relpath,
    )
    conn.commit()

    _maybe_finalize(conn, defense["id"], row["review_round"], job_id, lab_dir)
    return "done"


def _maybe_finalize(conn, defense_id: int, review_round: int, job_id: int, lab_dir: Path) -> None:
    """Tally once all five have reported. 4-of-5 strong_accept to pass."""
    reported = conn.execute(
        "SELECT COUNT(*) AS n FROM reviews WHERE target_type='defense' AND target_id=? "
        "AND review_round=?",
        (defense_id, review_round),
    ).fetchone()["n"]
    if reported < REVIEWER_COUNT:
        return

    strong = conn.execute(
        "SELECT COUNT(*) AS n FROM reviews WHERE target_type='defense' AND target_id=? "
        "AND review_round=? AND verdict='strong_accept'",
        (defense_id, review_round),
    ).fetchone()["n"]
    defense = conn.execute("SELECT * FROM defenses WHERE id = ?", (defense_id,)).fetchone()
    passed = strong >= STRONG_ACCEPT_THRESHOLD

    conn.execute(
        "UPDATE defenses SET status = ? WHERE id = ?",
        ("passed" if passed else "failed", defense_id),
    )
    if passed:
        conn.execute(
            "UPDATE students SET status = 'graduated' WHERE id = ?", (defense["student_id"],)
        )
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('propose_lab', 'student', ?, 'pending')",
            (defense["student_id"],),
        )
    else:
        # Back to work with the rationales attached; §3.4 leaves the
        # revise-or-not decision open rather than auto-resubmitting.
        conn.execute(
            "UPDATE students SET status = 'working' WHERE id = ?", (defense["student_id"],)
        )

    record_job_event(
        conn, job_id=job_id, actor_type="daemon", actor_id=None,
        event_type="defense_passed" if passed else "defense_failed",
        target_type="defense", target_id=defense_id,
    )
    conn.commit()


def execute_propose_lab_job(
    conn: sqlite3.Connection, job_id: int, backend: Backend, lab_dir: Path
) -> str:
    """A graduated student's supervisor proposes the lab they will lead.

    Creates a `lab_proposals` row in `pending_approval` and stops. §3.5
    makes this a human's decision, and auto-founding labs on a review vote
    is how a system silently becomes an unbounded number of labs nobody
    chose to start.
    """
    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=1800):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    student = conn.execute("SELECT * FROM students WHERE id = ?", (row["target_id"],)).fetchone()
    if student is None:
        return jobs.fail_job(conn, job_id, lease_id, f"no student with id={row['target_id']}")

    existing = conn.execute(
        "SELECT id FROM lab_proposals WHERE student_id = ?", (student["id"],)
    ).fetchone()
    if existing:
        jobs.complete_job(conn, job_id, lease_id)
        return "done"

    professor = conn.execute(
        "SELECT * FROM professors WHERE id = ?", (student["professor_id"],)
    ).fetchone()
    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (professor["lab_id"],)).fetchone()
    defense = conn.execute(
        "SELECT * FROM defenses WHERE student_id = ? AND status = 'passed' ORDER BY id DESC LIMIT 1",
        (student["id"],),
    ).fetchone()
    if defense is None:
        return jobs.fail_job(conn, job_id, lease_id, f"student {student['id']} has no passed defense")

    path = lab_dir / defense["path"]
    result = jobs.run_with_session(
        conn,
        job_id,
        backend,
        PROPOSAL_PROMPT_TEMPLATE.format(
            name=professor["name"],
            root_problem=lab["root_problem"],
            dissertation=(path.read_text(errors="replace")[:30000] if path.exists() else "(missing)"),
        ),
    )

    if result.rate_limited:
        jobs.record_rate_limit(conn, job_id, lease_id, result.retry_after_seconds)
        return "rate_limited"
    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)

    try:
        payload = extract_json_object(result.text)
        name, field, root_problem = (
            str(payload["name"]).strip(),
            str(payload["field"]).strip(),
            str(payload["root_problem"]).strip(),
        )
    except Exception as e:  # noqa: BLE001
        return jobs.fail_job(
            conn, job_id, lease_id, f"unusable lab proposal: {e} -- raw: {result.text[:300]}"
        )
    if not (name and field and root_problem):
        return jobs.fail_job(conn, job_id, lease_id, "lab proposal missing name, field or problem")

    conn.execute(
        "INSERT INTO lab_proposals (student_id, proposed_name, proposed_field, proposed_problem, status) "
        "VALUES (?, ?, ?, ?, 'pending_approval')",
        (student["id"], name, field, root_problem),
    )

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"
    record_job_event(
        conn, job_id=job_id, actor_type="professor", actor_id=professor["id"],
        event_type="lab_proposed", target_type="student", target_id=student["id"],
    )
    conn.commit()
    return "done"
