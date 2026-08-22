"""`autoprof status` -- the whole lab tree in one view.

docs/TASKS.md Phase 5. `lab list` only ever covered labs, so answering
"what is actually happening right now" meant opening the DB by hand.
This walks labs -> professor -> tasks -> student -> papers -> review
verdicts, plus the job queue, in one read-only pass.
"""

import argparse
import json
from pathlib import Path

from . import db, recovery

_VERDICT_MARK = {
    "strong_accept": "++",
    "accept": "+",
    "weak_accept": "~+",
    "weak_reject": "~-",
    "reject": "-",
    "strong_reject": "--",
}


def _render_reviews(conn, target_type: str, target_id: int, round_: int) -> str:
    rows = conn.execute(
        "SELECT verdict FROM reviews WHERE target_type=? AND target_id=? AND review_round=? "
        "ORDER BY reviewer_index",
        (target_type, target_id, round_),
    ).fetchall()
    if not rows:
        return ""
    marks = " ".join(_VERDICT_MARK.get(r["verdict"], r["verdict"]) for r in rows)
    strong = sum(1 for r in rows if r["verdict"] == "strong_accept")
    return f"  reviews[r{round_}]: {marks}  ({strong} strong_accept)"


def render_status(conn) -> str:
    out = []
    labs = conn.execute("SELECT * FROM labs ORDER BY id").fetchall()
    if not labs:
        out.append("(no labs yet -- run `autoprof create-prof`)")

    for lab in labs:
        professor = conn.execute(
            "SELECT * FROM professors WHERE id = ?", (lab["professor_id"],)
        ).fetchone()
        prof_desc = (
            f"{professor['name']} ({professor['field']})" if professor else "(no professor)"
        )
        out.append(f"LAB #{lab['id']}  [{lab['status']}]  {prof_desc}")
        out.append(f"  root problem: {lab['root_problem'][:160].strip()}...")

        review_line = _render_reviews(conn, "lab", lab["id"], lab["current_review_round"])
        if review_line:
            out.append(review_line)

        tasks = conn.execute(
            "SELECT * FROM tasks WHERE lab_id = ? ORDER BY id", (lab["id"],)
        ).fetchall()
        if not tasks:
            out.append("  (no tasks decomposed yet)")

        for task in tasks:
            out.append(f"  TASK #{task['id']} [{task['status']}] ({task['direction']}) {task['title']}")

            if task["assigned_student_id"] is not None:
                student = conn.execute(
                    "SELECT * FROM students WHERE id = ?", (task["assigned_student_id"],)
                ).fetchone()
                paused = " PAUSED" if student and student["paused_at"] else ""
                if student:
                    out.append(f"    student #{student['id']} [{student['status']}]{paused}")

            papers = conn.execute(
                "SELECT * FROM papers WHERE task_id = ? ORDER BY id", (task["id"],)
            ).fetchall()
            for paper in papers:
                out.append(
                    f"    PAPER #{paper['id']} [{paper['status']}] "
                    f"round {paper['review_round']}: {paper['title'][:80]}"
                )
                line = _render_reviews(conn, "paper", paper["id"], paper["review_round"])
                if line:
                    out.append("  " + line)
        out.append("")

    counts = conn.execute(
        "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status ORDER BY status"
    ).fetchall()
    if counts:
        summary = "  ".join(f"{r['status']}={r['n']}" for r in counts)
        out.append(f"JOBS: {summary}")

    by_kind = conn.execute(
        "SELECT kind, status, COUNT(*) AS n FROM jobs WHERE status IN ('pending', 'running') "
        "GROUP BY kind, status ORDER BY kind"
    ).fetchall()
    for row in by_kind:
        out.append(f"  {row['kind']}: {row['status']} x{row['n']}")

    # What each running job has actually produced. A long job and a hung one
    # are indistinguishable without this, and the difference decides whether
    # waiting is patience or a stall.
    live = conn.execute(
        "SELECT id, kind, target_id, started_at, progress_at, progress_tokens, "
        "progress_items FROM jobs WHERE status='running' ORDER BY id"
    ).fetchall()
    for row in live:
        if row["progress_at"]:
            work = (f"{row['progress_tokens'] or 0} tokens, "
                    f"{row['progress_items'] or 0} items, last seen {row['progress_at']}")
        else:
            work = "no output yet"
        out.append(
            f"  RUNNING job #{row['id']} ({row['kind']} -> {row['target_id']}) "
            f"since {row['started_at']}: {work}"
        )

    failed = conn.execute(
        "SELECT id, kind, last_error FROM jobs WHERE status='failed' ORDER BY id"
    ).fetchall()
    for row in failed:
        error = (row["last_error"] or "").splitlines()
        out.append(f"  FAILED job #{row['id']} ({row['kind']}): {error[0][:120] if error else ''}")

    # Failure memories (§18): what went wrong, grouped, with the rule that
    # would prevent a recurrence. Surfaced here because a failure nobody
    # reads teaches nobody anything.
    memories = conn.execute(
        "SELECT classification, COUNT(*) AS n, MAX(preventive_rule) AS rule "
        "FROM failure_memories WHERE resolved = 0 GROUP BY classification ORDER BY n DESC"
    ).fetchall()
    if memories:
        out.append("")
        out.append("FAILURE MEMORY (unresolved):")
        for row in memories:
            out.append(f"  {row['classification']} x{row['n']}")
            if row["rule"]:
                out.append(f"    -> {row['rule']}")

    return "\n".join(out)


def render_blocked(conn) -> str:
    """Jobs that stopped for a reason no retry can clear.

    A provider refusal is the motivating case: nothing about the run was
    wrong, so it leaves no failing test and no error a human would think
    to look for -- the lab simply stops having pending work. Lab #9's
    supervision was refused on cybersecurity-content grounds, went
    terminal, and the whole lab sat idle with no signal anywhere.

    Classified from jobs.last_error rather than read from the escalation
    events, so it reports failures that predate the event and cannot go
    blind if one was never written.
    """
    rows = conn.execute(
        "SELECT id, kind, target_type, target_id, last_error, completed_at "
        "FROM jobs WHERE status = 'failed' ORDER BY id DESC"
    ).fetchall()
    blocked = [
        (row, recovery.classify_failure(row["last_error"]))
        for row in rows
        if recovery.lookup(recovery.classify_failure(row["last_error"])).escalate
    ]
    if not blocked:
        return "No blocked jobs: nothing has failed in a class that needs a human."

    denied = sum(1 for _, c in blocked if c == recovery.MODEL_DENIED)
    head = f"{len(blocked)} blocked job(s) -- these will not retry on their own"
    if denied:
        head += f"; {denied} refused by the provider on content grounds"
    out = [head + ":", ""]
    for row, classification in blocked:
        out.append(
            f"  job {row['id']} [{row['kind']}] on {row['target_type']} "
            f"{row['target_id']} -- {classification}  ({row['completed_at']})"
        )
        rule = recovery.lookup(classification).preventive_rule
        if rule:
            out.append(f"    -> {rule}")
        lines = [ln for ln in (row["last_error"] or "").strip().splitlines() if ln.strip()]
        if lines:
            out.append(f"    {lines[-1][:160]}")
        out.append("")
    return "\n".join(out).rstrip()


def _cmd_status(args) -> int:
    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    print(render_blocked(conn) if args.blocked else render_status(conn))
    conn.close()
    return 0


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "status", help="Show the full lab tree: labs, tasks, students, papers, reviews, jobs."
    )
    p.add_argument("--db-path", type=Path, default=db.DEFAULT_DB_PATH)
    p.add_argument(
        "--blocked", action="store_true",
        help="Instead show only jobs that stopped in a class no retry can clear "
             "(provider content refusals, bad config, impossible tasks).",
    )
    p.set_defaults(func=_cmd_status)
