"""`autoprof create-prof` -- turn a raw idea into a Professor + Lab.

Takes whatever half-formed idea the human gives it and asks the
configured generation backend (Codex or Ollama Cloud, see
autoprof/backends/registry.py) to distill it into a professor identity
(name, field) and a rigorous root-problem statement -- the lab's "soul":
the enduring problem definition that gets decomposed into a tree of tasks
for years, per docs/DESIGN.md §1/§2.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from . import db
from .backends.base import Backend
from .backends.registry import default_registry

SOUL_PROMPT_TEMPLATE = """You are helping bootstrap a new research lab in the auto-prof system. \
A human has given you a raw, possibly half-formed idea for a hard problem. Your job is to \
distill it into:

1. A professor identity to lead this lab: a name and a field/domain label.
2. A rigorous, well-scoped ROOT PROBLEM STATEMENT. This becomes the professor's "soul" -- \
the enduring problem definition that will be decomposed into a tree of research tasks (each \
proved, disproved, or explored) over years of work. It must:
   - State the problem precisely enough that a task could be judged "resolved" against it.
   - Note what would count as proving it, disproving it, or making significant progress on it.
   - Be scoped so it is neither a single task in disguise (too narrow) nor something no task \
could ever close out (too vague).
   - Stay faithful to the human's actual idea -- sharpen and formalize it, don't replace it \
with a different problem you find more interesting.

Raw idea from the human:
<idea>
{idea}
</idea>

Respond with ONLY a JSON object, no markdown code fences, no commentary before or after, in \
exactly this shape:
{{"name": "...", "field": "...", "root_problem": "..."}}
"""

MEMORY_SEED_TEMPLATE = """# Professor: {name}

Field: {field}

## Root Problem (the lab's soul)

{root_problem}

## Status

Newly created. No tasks decomposed yet, no papers submitted, no decisions made.

## Strategy

Not yet formed. The first job against this professor should be an initial
decomposition of the root problem into a tree of tasks (see docs/DESIGN.md §1/§3).
"""


class SoulGenerationError(RuntimeError):
    pass


def _extract_json_object(text: str) -> dict:
    """claude -p sometimes wraps JSON in fences or adds stray whitespace
    despite instructions -- strip fences if present, then parse."""
    text = text.strip()
    fence_match = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    return json.loads(text)


def generate_soul(idea: str, backend: Backend) -> dict:
    """Call `backend` to turn a raw idea into {name, field, root_problem}.

    `backend` is any Backend (Codex, Ollama Cloud, ...) -- this function
    doesn't know or care which, matching the "keep it modular" goal.
    """
    prompt = SOUL_PROMPT_TEMPLATE.format(idea=idea.strip())
    result = backend.run(prompt)

    if result.rate_limited:
        raise SoulGenerationError(
            f"backend '{backend.name}' is rate-limited"
            + (f" (retry after {result.retry_after_seconds}s)" if result.retry_after_seconds else "")
        )
    if result.is_error:
        raise SoulGenerationError(f"backend '{backend.name}' error: {result.error}")

    try:
        soul = _extract_json_object(result.text)
    except json.JSONDecodeError as e:
        raise SoulGenerationError(
            f"model response was not the expected JSON object: {result.text[:500]}"
        ) from e

    missing = {"name", "field", "root_problem"} - soul.keys()
    if missing:
        raise SoulGenerationError(f"model response missing required keys: {missing} -- got {soul}")

    return soul


def persist_professor(conn, name: str, field: str, root_problem: str, lab_dir: Path) -> tuple[int, int]:
    """Create the professor + lab rows and seed memory.md.

    Matches the bootstrap sequence validated against docs/schema.sql:
    professors.lab_id is nullable (labs.professor_id is NOT NULL and
    references professors), so the professor row is created first with a
    placeholder, then the lab, then the professor is backfilled.
    """
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO professors (lab_id, name, field, status, memory_path) "
        "VALUES (NULL, ?, ?, 'active', 'pending')",
        (name, field),
    )
    professor_id = cur.lastrowid

    cur.execute(
        # 'pending_review', not 'active': the root problem hasn't been
        # vetted yet (autoprof/lab_review.py) -- the daemon won't dispatch
        # any work against this lab until a review passes and propagates
        # it to 'active'.
        "INSERT INTO labs (professor_id, root_problem, status) VALUES (?, ?, 'pending_review')",
        (professor_id, root_problem),
    )
    lab_id = cur.lastrowid

    memory_rel_path = f"lab/{lab_id}/professors/{professor_id}/memory.md"
    cur.execute(
        "UPDATE professors SET lab_id = ?, memory_path = ? WHERE id = ?",
        (lab_id, memory_rel_path, professor_id),
    )

    memory_abs_path = lab_dir / str(lab_id) / "professors" / str(professor_id) / "memory.md"
    memory_abs_path.parent.mkdir(parents=True, exist_ok=True)
    memory_abs_path.write_text(
        MEMORY_SEED_TEMPLATE.format(name=name, field=field, root_problem=root_problem)
    )

    conn.commit()
    return professor_id, lab_id


def run(args: argparse.Namespace) -> int:
    idea = args.idea
    if idea is None:
        if sys.stdin.isatty():
            print("Enter the idea, then Ctrl-D:", file=sys.stderr)
        idea = sys.stdin.read()
    if not idea.strip():
        print("error: no idea provided", file=sys.stderr)
        return 1

    registry = default_registry(args.config_path)
    backend = registry.get_backend("professor_decompose")
    print(f"Distilling idea into a research problem via '{backend.name}' ...", file=sys.stderr)
    try:
        soul = generate_soul(idea, backend)
    except SoulGenerationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print()
    print(f"Professor: {soul['name']}  ({soul['field']})")
    print()
    print("Root problem (the lab's soul):")
    print(f"  {soul['root_problem']}")
    print()

    if not args.yes:
        answer = input("Create this professor and lab? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted -- nothing written.", file=sys.stderr)
            return 1

    if args.dry_run:
        print("(--dry-run: not writing to the database)", file=sys.stderr)
        return 0

    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)
    professor_id, lab_id = persist_professor(
        conn, soul["name"], soul["field"], soul["root_problem"], args.lab_dir
    )
    conn.close()

    print(f"Created professor id={professor_id}, lab id={lab_id}.")
    print(f"Memory seeded at lab/{lab_id}/professors/{professor_id}/memory.md")
    return 0


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "create-prof",
        help="Turn a raw idea into a Professor + Lab (the idea becomes the professor's root-problem soul).",
    )
    p.add_argument(
        "idea",
        nargs="?",
        help="The raw idea, as free text. If omitted, reads from stdin.",
    )
    p.add_argument(
        "--yes", "-y", action="store_true", help="Skip the confirmation prompt."
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate and print the soul, but don't write to the database.",
    )
    p.add_argument(
        "--db-path",
        type=Path,
        default=db.DEFAULT_DB_PATH,
        help="Path to autoprof.db (default: repo root).",
    )
    p.add_argument(
        "--lab-dir",
        type=Path,
        default=db.LAB_DIR,
        help="Path to the lab/ artifact directory (default: repo root).",
    )
    p.add_argument(
        "--config-path",
        type=Path,
        default=db.REPO_ROOT / "autoprof.toml",
        help="Path to autoprof.toml backend config (default: repo root).",
    )
    p.set_defaults(func=run)
