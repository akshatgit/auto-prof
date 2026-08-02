"""`autoprof daemon run` -- actually start the daemon."""

import argparse
from pathlib import Path

from . import db
from .backends.registry import default_registry
from .daemon import SingleInstanceLock, run_daemon
from .decompose import execute_professor_decompose_job
from .lab_review import execute_lab_review_job
from .paper import (
    execute_student_revise_paper_job,
    execute_student_work_job,
    execute_student_write_paper_job,
)
from .paper_review import execute_paper_review_job
from .supervision import execute_professor_supervision_job
from .prompt_builders import default_builders

# Every job kind in the research lifecycle needs more than one artifact
# write to express its outcome (create task rows, insert a papers row,
# tally a review round), so all of them take the special-handler path;
# prompt_builders' generic path remains for future single-artifact kinds
# like memory_compact.
SPECIAL_HANDLERS = {
    "lab_review": execute_lab_review_job,
    "professor_decompose": execute_professor_decompose_job,
    "student_work": execute_student_work_job,
    "student_write_paper": execute_student_write_paper_job,
    "student_revise_paper": execute_student_revise_paper_job,
    "paper_review": execute_paper_review_job,
    "professor_supervision": execute_professor_supervision_job,
}


def _cmd_run(args) -> int:
    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    registry = default_registry(args.config_path)

    lock = SingleInstanceLock(args.lock_path)
    if not lock.acquire():
        print(f"error: another autoprof daemon already holds {args.lock_path}")
        return 1

    def _log_tick(tick: int, stats: dict, delay) -> None:
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status='pending'"
        ).fetchone()["n"]
        running = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE status='running'"
        ).fetchone()["n"]
        sleeping = "" if delay is None else f" sleeping={delay:.0f}s"
        print(
            f"[tick {tick}] dispatched={stats['dispatched']} "
            f"reclaimed={stats['reclaimed']} pending={pending} running={running}{sleeping}",
            flush=True,
        )

    try:
        mode = "single tick" if args.once else f"loop (interval={args.interval}s)"
        print(f"autoprof daemon starting ({mode}, budget={args.budget}/tick)", flush=True)
        run_daemon(
            conn,
            registry=registry,
            prompt_builders=default_builders(),
            lab_dir=args.lab_dir,
            budget_cap=args.budget,
            default_interval=args.interval,
            once=args.once,
            max_ticks=args.max_ticks,
            special_handlers=SPECIAL_HANDLERS,
            on_tick=_log_tick,
        )
    except KeyboardInterrupt:
        print("\nautoprof daemon stopping (Ctrl-C)")
    finally:
        lock.release()
        conn.close()
    return 0


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser("daemon", help="Run the autoprof job-processing daemon.")
    sub = p.add_subparsers(dest="daemon_command", required=True)

    run_p = sub.add_parser("run", help="Start the daemon.")
    run_p.add_argument("--db-path", type=Path, default=db.DEFAULT_DB_PATH)
    run_p.add_argument("--lab-dir", type=Path, default=db.LAB_DIR)
    run_p.add_argument(
        "--config-path", type=Path, default=db.REPO_ROOT / "autoprof.toml"
    )
    run_p.add_argument(
        "--lock-path", type=Path, default=db.REPO_ROOT / "autoprof.lock"
    )
    run_p.add_argument(
        "--interval", type=float, default=300.0, help="Idle heartbeat interval in seconds."
    )
    run_p.add_argument(
        "--budget", type=int, default=10, help="Max jobs dispatched per tick."
    )
    run_p.add_argument(
        "--once", action="store_true", help="Run exactly one tick, then exit."
    )
    run_p.add_argument(
        "--max-ticks", type=int, default=None, help="Stop after N ticks (mainly for testing)."
    )
    run_p.set_defaults(func=_cmd_run)
