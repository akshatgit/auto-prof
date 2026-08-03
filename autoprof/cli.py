import argparse
import sys

from . import (
    bootstrap_cli,
    create_prof,
    daemon_cli,
    lab_cli,
    references_cli,
    status_cli,
    student_cli,
    watch_cli,
    webserver_cli,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoprof")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_prof.add_subparser(subparsers)
    bootstrap_cli.add_subparser(subparsers)
    student_cli.add_subparser(subparsers)
    lab_cli.add_subparser(subparsers)
    status_cli.add_subparser(subparsers)
    references_cli.add_subparser(subparsers)
    watch_cli.add_subparser(subparsers)
    daemon_cli.add_subparser(subparsers)
    webserver_cli.add_subparser(subparsers)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
