import argparse
import sys

from . import create_prof, daemon_cli, lab_cli, student_cli, webserver_cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autoprof")
    subparsers = parser.add_subparsers(dest="command", required=True)
    create_prof.add_subparser(subparsers)
    student_cli.add_subparser(subparsers)
    lab_cli.add_subparser(subparsers)
    daemon_cli.add_subparser(subparsers)
    webserver_cli.add_subparser(subparsers)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
