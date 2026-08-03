"""`autoprof lab ...` -- CLI surface over autoprof/lab_review.py."""

import argparse
import sys
from pathlib import Path

from . import create_prof, db, lab_review


def common_args() -> argparse.ArgumentParser:
    """A `parents=` parser carrying --db-path with a suppressed default.

    Shared with student_cli via its own import so both command groups
    accept the flag in the same two positions.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--db-path", type=Path, default=argparse.SUPPRESS)
    return parser


def _cmd_review_request(args) -> int:
    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    try:
        job_ids = lab_review.request_lab_review(conn, args.lab_id)
    except lab_review.LabReviewError as e:
        print(f"error: {e}")
        return 1
    print(f"requested review for lab #{args.lab_id}: jobs {job_ids}")
    conn.close()
    return 0


def _cmd_revise(args) -> int:
    problem = args.problem
    if problem is None:
        if sys.stdin.isatty():
            print("Enter the revised root problem, then Ctrl-D:", file=sys.stderr)
        problem = sys.stdin.read()

    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    try:
        job_ids = lab_review.revise_root_problem(conn, args.lab_id, problem)
    except lab_review.LabReviewError as e:
        print(f"error: {e}")
        conn.close()
        return 1
    round_ = conn.execute(
        "SELECT current_review_round FROM labs WHERE id = ?", (args.lab_id,)
    ).fetchone()["current_review_round"]
    print(f"lab #{args.lab_id} revised; round {round_} review requested: jobs {job_ids}")
    conn.close()
    return 0


def _cmd_proposals(args) -> int:
    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    rows = conn.execute(
        "SELECT * FROM lab_proposals WHERE status='pending_approval' ORDER BY id"
    ).fetchall()
    if not rows:
        print("(no lab proposals awaiting approval)")
    for row in rows:
        print(f"#{row['id']}  from student {row['student_id']}")
        print(f"    {row['proposed_name']}  ({row['proposed_field']})")
        print(f"    {row['proposed_problem'][:300]}")
    conn.close()
    return 0


def _cmd_approve(args) -> int:
    """Approve a proposal: create the professor, the lab, and link them.

    All three writes in ONE transaction. docs/DESIGN.md §3.5 and the
    lab_proposals CHECK constraint both require that an 'approved' row
    always carries both resulting ids -- a partially-applied approval is
    the one bad state the schema explicitly refuses.
    """
    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    proposal = conn.execute(
        "SELECT * FROM lab_proposals WHERE id = ?", (args.proposal_id,)
    ).fetchone()
    if proposal is None:
        print(f"error: no proposal #{args.proposal_id}")
        conn.close()
        return 1
    if proposal["status"] != "pending_approval":
        print(f"error: proposal #{args.proposal_id} is already {proposal['status']}")
        conn.close()
        return 1

    if args.reject:
        conn.execute(
            "UPDATE lab_proposals SET status='rejected', decided_at=datetime('now') WHERE id=?",
            (args.proposal_id,),
        )
        conn.commit()
        print(f"proposal #{args.proposal_id} rejected")
        conn.close()
        return 0

    try:
        with conn:
            professor_id, lab_id = create_prof.persist_professor(
                conn,
                proposal["proposed_name"],
                proposal["proposed_field"],
                proposal["proposed_problem"],
                args.lab_dir,
            )
            conn.execute(
                "UPDATE professors SET parent_student_id = ? WHERE id = ?",
                (proposal["student_id"], professor_id),
            )
            conn.execute(
                "UPDATE lab_proposals SET status='approved', resulting_professor_id=?, "
                "resulting_lab_id=?, decided_at=datetime('now') WHERE id=?",
                (professor_id, lab_id, args.proposal_id),
            )
    except Exception as e:  # noqa: BLE001
        print(f"error: approval failed, nothing written: {e}")
        conn.close()
        return 1

    job_ids = lab_review.request_lab_review(conn, lab_id)
    print(f"approved: professor #{professor_id} now leads lab #{lab_id}")
    print(f"lab is 'pending_review'; round-1 review requested: jobs {job_ids}")
    conn.close()
    return 0


def _cmd_list(args) -> int:
    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    for row in conn.execute("SELECT * FROM labs ORDER BY id"):
        print(f"#{row['id']}  status={row['status']}  professor_id={row['professor_id']}")
        print(f"    {row['root_problem'][:120]}")
    conn.close()
    return 0


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser("lab", help="Inspect labs or request a lab review.")
    p.add_argument("--db-path", type=Path, default=db.DEFAULT_DB_PATH)
    sub = p.add_subparsers(dest="lab_command", required=True)

    # Accepted on either side of the subcommand: `lab --db-path X list` and
    # `lab list --db-path X` both work. Without the leaf copy only the
    # former parses, which is the opposite of `create-prof`/`daemon run`,
    # where the flag is on the leaf -- an inconsistency that reads as a
    # bug the first time you hit it. SUPPRESS is what makes the pair safe:
    # the leaf only sets db_path when explicitly given, so it can't stomp
    # the group-level value with its own default.
    common = common_args()

    sp = sub.add_parser("list", help="List all labs.", parents=[common])
    sp.set_defaults(func=_cmd_list)

    sp = sub.add_parser(
        "review-request",
        help="Request review of a lab's root problem (3 independent Codex reviewers).",
        parents=[common],
    )
    sp.add_argument("lab_id", type=int)
    sp.set_defaults(func=_cmd_review_request)

    sp = sub.add_parser("proposals", help="List lab proposals awaiting approval.", parents=[common])
    sp.set_defaults(func=_cmd_proposals)

    sp = sub.add_parser(
        "approve",
        help="Approve (or --reject) a lab proposal from a graduated student.",
        parents=[common],
    )
    sp.add_argument("proposal_id", type=int)
    sp.add_argument("--reject", action="store_true")
    sp.add_argument("--lab-dir", type=Path, default=db.LAB_DIR)
    sp.set_defaults(func=_cmd_approve)

    sp = sub.add_parser(
        "revise",
        help="Replace a rejected lab's root problem and start a fresh review round.",
        parents=[common],
    )
    sp.add_argument("lab_id", type=int)
    sp.add_argument(
        "problem",
        nargs="?",
        help="The revised root problem. If omitted, reads from stdin.",
    )
    sp.set_defaults(func=_cmd_revise)
