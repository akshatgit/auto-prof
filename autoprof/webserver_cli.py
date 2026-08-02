"""`autoprof web run` -- start the read-only web UI."""

import argparse
from pathlib import Path

from . import db
from .webserver import run_server


def _cmd_run(args) -> int:
    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    conn.close()
    run_server(args.db_path, host=args.host, port=args.port)
    return 0


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser("web", help="Read-only web UI over the autoprof DB.")
    sub = p.add_subparsers(dest="web_command", required=True)

    run_p = sub.add_parser("run", help="Start the web server.")
    run_p.add_argument("--db-path", type=Path, default=db.DEFAULT_DB_PATH)
    run_p.add_argument("--host", default="127.0.0.1")
    run_p.add_argument("--port", type=int, default=8765)
    run_p.set_defaults(func=_cmd_run)
