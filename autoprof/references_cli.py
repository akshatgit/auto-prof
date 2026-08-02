"""`autoprof ref ...` -- inspect and curate the shared reference bank."""

import argparse
from pathlib import Path

from . import db, references


def _cmd_list(args) -> int:
    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    rows = conn.execute(
        "SELECT * FROM reference_works WHERE (? IS NULL OR status = ?) ORDER BY status, id",
        (args.status, args.status),
    ).fetchall()
    if not rows:
        print("(reference bank is empty)")
    for row in rows:
        cited_by = conn.execute(
            "SELECT COUNT(*) AS n FROM reference_citations WHERE reference_id = ?", (row["id"],)
        ).fetchone()["n"]
        print(f"[{row['id']}] {row['status']:<10} {row['kind']:<14} {row['title'][:60]}")
        print(f"      {row['authors'][:70]}  {row['identifier'] or ''}  cited by {cited_by}")
    conn.close()
    return 0


def _cmd_add(args) -> int:
    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    try:
        ref_id = references.add_reference(
            conn,
            title=args.title,
            authors=args.authors,
            identifier=args.identifier,
            venue=args.venue,
            year=args.year,
            status=references.VERIFIED if args.verified else references.UNVERIFIED,
        )
    except references.ReferenceError as e:
        print(f"error: {e}")
        conn.close()
        return 1
    print(f"reference #{ref_id} added")
    conn.close()
    return 0


def _cmd_verify(args) -> int:
    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    status = references.DISPUTED if args.dispute else references.VERIFIED
    ok = references.set_status(conn, args.reference_id, status, args.note)
    if not ok:
        print(f"error: no reference #{args.reference_id}")
        conn.close()
        return 1
    print(f"reference #{args.reference_id} marked {status}")

    if status == references.DISPUTED:
        # A disputed reference is only actionable if you know what leaned
        # on it -- that is what the citation edges are for.
        affected = references.contaminated_papers(conn, args.reference_id)
        if affected:
            print("papers citing it (revisit these):")
            for paper in affected:
                print(f"  paper #{paper['id']} [{paper['status']}] {paper['title'][:60]}")
    conn.close()
    return 0


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser("ref", help="The lab's shared reference bank.")
    sub = p.add_subparsers(dest="ref_command", required=True)

    sp = sub.add_parser("list", help="List references.")
    sp.add_argument("--status", choices=["unverified", "verified", "disputed"], default=None)
    sp.add_argument("--db-path", type=Path, default=db.DEFAULT_DB_PATH)
    sp.set_defaults(func=_cmd_list)

    sp = sub.add_parser("add", help="Add a work to the bank.")
    sp.add_argument("title")
    sp.add_argument("authors")
    sp.add_argument("--identifier", help="arXiv id, DOI or URL.")
    sp.add_argument("--venue")
    sp.add_argument("--year", type=int)
    sp.add_argument(
        "--verified",
        action="store_true",
        help="Mark verified immediately (you have checked the real record).",
    )
    sp.add_argument("--db-path", type=Path, default=db.DEFAULT_DB_PATH)
    sp.set_defaults(func=_cmd_add)

    sp = sub.add_parser("verify", help="Mark a reference verified, or --dispute it.")
    sp.add_argument("reference_id", type=int)
    sp.add_argument("--dispute", action="store_true", help="Mark disputed and list affected papers.")
    sp.add_argument("--note")
    sp.add_argument("--db-path", type=Path, default=db.DEFAULT_DB_PATH)
    sp.set_defaults(func=_cmd_verify)
