"""`autoprof lab ...` -- CLI surface over autoprof/lab_review.py."""

import argparse
import sys
from pathlib import Path

from . import db, lab_review


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
