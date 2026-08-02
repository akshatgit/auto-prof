"""`autoprof daemon run` -- actually start the daemon."""

import argparse
from pathlib import Path

from . import db
from .backends.registry import default_registry
from .daemon import SingleInstanceLock, run_daemon
from .lab_review import execute_lab_review_job
from .prompt_builders import default_builders

SPECIAL_HANDLERS = {"lab_review": execute_lab_review_job}


def _cmd_run(args) -> int:
    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    registry = default_registry(args.config_path)

    lock = SingleInstanceLock(args.lock_path)
    if not lock.acquire():
        print(f"error: another autoprof daemon already holds {args.lock_path}")
        return 1

    try:
        mode = "single tick" if args.once else f"loop (interval={args.interval}s)"
        print(f"autoprof daemon starting ({mode}, budget={args.budget}/tick)")
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
