"""`autoprof lab ...` -- CLI surface over autoprof/lab_review.py."""

import argparse
from pathlib import Path

from . import db, lab_review


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

    sp = sub.add_parser("list", help="List all labs.")
    sp.set_defaults(func=_cmd_list)

    sp = sub.add_parser(
        "review-request",
        help="Request review of a lab's root problem (3 independent Codex reviewers).",
    )
    sp.add_argument("lab_id", type=int)
    sp.set_defaults(func=_cmd_review_request)
