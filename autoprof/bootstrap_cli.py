"""`autoprof bootstrap` -- found a lab from your own research.

`create-prof` distils a lab from a sentence. This distils one from the
papers and notes you already have, so the root problem is grounded in what
you actually established rather than in what a model imagines the field
contains.

The documents are ingested BEFORE the professor is generated, and their
text is fed into the distillation -- that ordering is the whole point. A
root problem written first and "informed by" the corpus afterwards would
just be the sentence-based path with extra steps.
"""

import argparse
import sys
from pathlib import Path

from . import create_prof, db, ingest, lab_review, references

BOOTSTRAP_PROMPT_TEMPLATE = """You are founding a research lab around a body of work that \
already exists. The founder has uploaded their own research below.

{corpus}

{idea}

Read the uploaded research carefully and distil it into:

1. A professor identity to lead this lab: a name and a field/domain label, drawn from what the \
uploaded work is actually about.
2. A rigorous ROOT PROBLEM STATEMENT that becomes this lab's enduring question.

The root problem must:
- Build on what the uploaded research ESTABLISHED. Do not restate their results as open \
questions -- treat established results as the ground the lab stands on, and pose the question \
they leave open.
- Be honest about what the uploaded work does and does not settle. If a result is partial, say \
what remains.
- State the problem precisely enough that a future task could be judged resolved against it.
- Note what would count as proving it, disproving it, or making significant progress.
- Be scoped so it is neither a single task in disguise nor something no sequence of tasks could \
close out.
- Stay faithful to the founder's actual direction. Sharpen and formalise it; do not substitute a \
different problem you find more interesting.

Respond with ONLY a JSON object, no markdown code fences, no commentary before or after, in \
exactly this shape:
{{"name": "...", "field": "...", "root_problem": "...", "builds_on": "..."}}
where "builds_on" is a short statement of which uploaded results the root problem takes as \
established.
"""


def _cmd_bootstrap(args) -> int:
    sources = [Path(p) for p in args.paths]
    missing = [str(p) for p in sources if not p.is_file()]
    if missing:
        print(f"error: not a file: {', '.join(missing)}", file=sys.stderr)
        return 1

    conn = db.connect(args.db_path)
    db.ensure_initialized(conn)

    # Ingest into a staging lab id of 0 first? No -- the lab does not exist
    # yet and source_documents.lab_id is NOT NULL. Extract the text up
    # front instead, so a corpus that cannot be read fails BEFORE a
    # half-built lab exists in the database.
    extracted = []
    for source in sources:
        try:
            text = ingest.extract_text(source).strip()
        except ingest.IngestError as e:
            print(f"error: {e}", file=sys.stderr)
            conn.close()
            return 1
        if not text:
            print(f"error: {source.name}: no readable text extracted", file=sys.stderr)
            conn.close()
            return 1
        extracted.append((source, text))
    print(f"Read {len(extracted)} document(s), "
          f"{sum(len(t.split()) for _, t in extracted):,} words total.", file=sys.stderr)

    corpus = "\n\n".join(
        f"--- {source.name} ---\n{text[: args.chars_per_doc]}"
        + ("\n[... truncated ...]" if len(text) > args.chars_per_doc else "")
        for source, text in extracted
    )
    corpus = f"<uploaded_research>\n{corpus}\n</uploaded_research>"

    registry = create_prof.default_registry(args.config_path)
    backend = registry.get_backend("professor_decompose")
    print(f"Distilling a root problem from your research via '{backend.name}' ...", file=sys.stderr)

    idea_block = (
        f"The founder also says this about the direction they want the lab to take:\n<idea>\n"
        f"{args.idea.strip()}\n</idea>"
        if args.idea
        else "The founder gave no additional direction; work only from the uploaded research."
    )

    result = backend.run(BOOTSTRAP_PROMPT_TEMPLATE.format(corpus=corpus, idea=idea_block))
    if result.rate_limited or result.is_error or not result.text.strip():
        print(f"error: backend failed: {result.error or 'rate limited or empty'}", file=sys.stderr)
        conn.close()
        return 1

    try:
        soul = create_prof.extract_json_object(result.text)
    except Exception as e:  # noqa: BLE001
        print(f"error: unusable response: {e}\n{result.text[:400]}", file=sys.stderr)
        conn.close()
        return 1

    missing_keys = {"name", "field", "root_problem"} - soul.keys()
    if missing_keys:
        print(f"error: response missing {sorted(missing_keys)}", file=sys.stderr)
        conn.close()
        return 1

    print()
    print(f"Professor: {soul['name']}  ({soul['field']})")
    print()
    print("Root problem:")
    print(f"  {soul['root_problem']}")
    if soul.get("builds_on"):
        print()
        print("Builds on your uploaded results:")
        print(f"  {soul['builds_on']}")
    print()

    if not args.yes and input("Create this professor and lab? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Aborted -- nothing written.", file=sys.stderr)
        conn.close()
        return 1

    professor_id, lab_id = create_prof.persist_professor(
        conn, soul["name"], soul["field"], soul["root_problem"], args.lab_dir
    )
    print(f"Created professor id={professor_id}, lab id={lab_id}.")

    ingested, errors = ingest.ingest_all(conn, lab_id, sources, args.lab_dir)
    for error in errors:
        print(f"  warning: {error}", file=sys.stderr)
    for doc in ingested:
        marker = " (duplicate, already ingested)" if doc["duplicate"] else ""
        print(f"  source #{doc['id']}: {doc['title'][:64]} [{doc['word_count']} words]{marker}")

    if not args.no_references:
        seeded = references.seed_from_root_problem(
            conn, backend, soul["root_problem"], soul["field"]
        )
        if seeded:
            conn.execute(
                "INSERT INTO jobs (kind, target_type, target_id, status) "
                "VALUES ('reference_verify', 'lab', ?, 'pending')",
                (lab_id,),
            )
            conn.commit()
            print(f"  seeded {len(seeded)} candidate reference(s), pending verification")

    job_ids = lab_review.request_lab_review(conn, lab_id)
    print(
        f"Lab is 'pending_review'; requested round-1 review "
        f"({lab_review.REVIEWER_COUNT} independent reviewers, "
        f"{lab_review.STRONG_ACCEPT_THRESHOLD}-of-{lab_review.REVIEWER_COUNT} to pass): "
        f"jobs {job_ids}"
    )
    print("Start the daemon to run them: `autoprof daemon run`")
    conn.close()
    return 0


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "bootstrap",
        help="Found a lab from your own research documents (md, txt, html, tex, pdf).",
    )
    p.add_argument("paths", nargs="+", help="Research documents to upload.")
    p.add_argument(
        "--idea",
        help="Optional steer: what direction you want the lab to take beyond the documents.",
    )
    p.add_argument("--yes", "-y", action="store_true", help="Skip the confirmation prompt.")
    p.add_argument(
        "--chars-per-doc",
        type=int,
        default=12000,
        help="How much of each document to send when distilling (default 12000).",
    )
    p.add_argument("--no-references", action="store_true", help="Skip seeding the reference bank.")
    p.add_argument("--db-path", type=Path, default=db.DEFAULT_DB_PATH)
    p.add_argument("--lab-dir", type=Path, default=db.LAB_DIR)
    p.add_argument("--config-path", type=Path, default=db.REPO_ROOT / "autoprof.toml")
    p.set_defaults(func=_cmd_bootstrap)
