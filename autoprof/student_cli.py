"""`autoprof student ...` -- CLI surface over autoprof/student_ctl.py."""

import argparse
import sys
from pathlib import Path

from . import db, student_ctl


def _cmd_list(args) -> int:
    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    for row in student_ctl.list_students(conn):
        paused = " [PAUSED]" if row["paused_at"] else ""
        print(f"#{row['id']}  status={row['status']}{paused}  task_id={row['task_id']}")
    conn.close()
    return 0


def _cmd_show(args) -> int:
    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    try:
        row = student_ctl.get_student(conn, args.student_id)
    except student_ctl.StudentNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for key in row.keys():
        print(f"{key}: {row[key]}")
    conn.close()
    return 0


def _cmd_stop(args) -> int:
    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    try:
        changed = student_ctl.stop_student(conn, args.student_id)
    except student_ctl.StudentNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"student #{args.student_id} {'stopped' if changed else 'already stopped'}")
    conn.close()
    return 0


def _cmd_resume(args) -> int:
    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    try:
        changed = student_ctl.resume_student(conn, args.student_id)
    except student_ctl.StudentNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"student #{args.student_id} {'resumed' if changed else 'was not stopped'}")
    conn.close()
    return 0


def _cmd_edit(args) -> int:
    if args.status is None and args.memory_file is None:
        print("error: pass --status and/or --memory-file", file=sys.stderr)
        return 1
    memory_text = None
    if args.memory_file is not None:
        memory_text = Path(args.memory_file).read_text()

    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    try:
        student_ctl.edit_student(
            conn, args.student_id, status=args.status, memory_text=memory_text, lab_dir=db.LAB_DIR
        )
    except student_ctl.StudentControlError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"student #{args.student_id} updated")
    conn.close()
    return 0


def _cmd_replay(args) -> int:
    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    try:
        new_job_id = student_ctl.replay_job(conn, args.job_id)
    except student_ctl.StudentControlError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"replayed job #{args.job_id} as new job #{new_job_id} (status=pending)")
    conn.close()
    return 0


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser("student", help="Inspect or manually control a student.")
    p.add_argument("--db-path", type=Path, default=db.DEFAULT_DB_PATH)
    sub = p.add_subparsers(dest="student_command", required=True)

    sp = sub.add_parser("list", help="List all students.")
    sp.set_defaults(func=_cmd_list)

    sp = sub.add_parser("show", help="Show one student's full state.")
    sp.add_argument("student_id", type=int)
    sp.set_defaults(func=_cmd_show)

    sp = sub.add_parser("stop", help="Pause a student (idempotent).")
    sp.add_argument("student_id", type=int)
    sp.set_defaults(func=_cmd_stop)

    sp = sub.add_parser("resume", help="Un-pause a student (idempotent).")
    sp.add_argument("student_id", type=int)
    sp.set_defaults(func=_cmd_resume)

    sp = sub.add_parser("edit", help="Manually override a student's status and/or memory.md.")
    sp.add_argument("student_id", type=int)
    sp.add_argument("--status", default=None, help=f"One of {sorted(student_ctl.VALID_STUDENT_STATUSES)}")
    sp.add_argument("--memory-file", default=None, help="Path to a file whose contents replace memory.md")
    sp.set_defaults(func=_cmd_edit)

    sp = sub.add_parser("replay", help="Re-run a past (done/failed) job as a new pending job.")
    sp.add_argument("job_id", type=int)
    sp.set_defaults(func=_cmd_replay)


def _dispatch(args) -> int:
    return args.func(args)
