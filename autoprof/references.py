"""The lab's shared reference bank.

A citation with a fabricated title reached three separate papers in one
run before a reviewer thought to look up the actual record. The students
had no authoritative bibliography, so each invented plausible-looking
references independently, and the bad one propagated from the professor's
root problem into everything downstream.

This is the fix: one global store of works the lab may cite, with an
explicit verification status, plus provenance edges recording which paper
cited what. Three properties matter:

- **Global, not lab-scoped.** A reference is a fact about the world.
  Per-lab copies let two labs hold different titles for the same work,
  which is exactly how the bad citation survived.
- **Unverified until checked.** `verified` means someone confirmed the
  work exists AND that title/authors/venue match the real record. Students
  cite verified entries; anything else must be declared an assumption
  rather than dressed up as a source.
- **Citations are edges, not prose.** When a reference turns out to be
  wrong, `contaminated_papers` names every paper that leaned on it instead
  of leaving someone to grep.

Accepted papers enrol automatically, which is what makes this a shared
memory that accumulates across labs and across sessions rather than a
static bibliography.
"""

import sqlite3

UNVERIFIED = "unverified"
VERIFIED = "verified"
DISPUTED = "disputed"


class ReferenceError(RuntimeError):
    pass


def add_reference(
    conn: sqlite3.Connection,
    title: str,
    authors: str,
    identifier: str | None = None,
    venue: str | None = None,
    year: int | None = None,
    kind: str = "external_work",
    status: str = UNVERIFIED,
    notes: str | None = None,
    paper_id: int | None = None,
) -> int:
    """Add a work, or return the existing row when `identifier` is already
    known.

    Returning rather than raising on a duplicate identifier is deliberate:
    two students citing the same arXiv id is normal and should converge on
    one row, not fail. It is also the mechanism that stops the same work
    being entered twice under two different titles.
    """
    if not title.strip() or not authors.strip():
        raise ReferenceError("a reference needs at least a title and authors")

    if identifier:
        existing = conn.execute(
            "SELECT * FROM reference_works WHERE identifier = ?", (identifier,)
        ).fetchone()
        if existing:
            return existing["id"]

    # verified_at is computed by SQLite, not passed as a bound value -- a
    # bound "datetime('now')" would be stored as that literal string.
    cur = conn.execute(
        "INSERT INTO reference_works (kind, title, authors, venue, year, identifier, "
        "paper_id, status, notes, verified_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, "
        "CASE WHEN ? = 'verified' THEN datetime('now') ELSE NULL END)",
        (
            kind,
            title.strip(),
            authors.strip(),
            venue,
            year,
            identifier,
            paper_id,
            status,
            notes,
            status,
        ),
    )
    conn.commit()
    return cur.lastrowid


def set_status(conn: sqlite3.Connection, reference_id: int, status: str, notes: str | None = None) -> bool:
    """Mark a reference verified or disputed.

    Disputing one does not delete it: papers already cite it, and the
    citation edges are how those papers get found. A deleted reference
    would silently orphan that provenance.
    """
    if status not in (UNVERIFIED, VERIFIED, DISPUTED):
        raise ReferenceError(f"unknown status {status!r}")
    cur = conn.execute(
        "UPDATE reference_works SET status = ?, notes = COALESCE(?, notes), "
        "verified_at = CASE WHEN ? = 'verified' THEN datetime('now') ELSE verified_at END "
        "WHERE id = ?",
        (status, notes, status, reference_id),
    )
    conn.commit()
    return cur.rowcount == 1


def register_accepted_paper(conn: sqlite3.Connection, paper_id: int) -> int | None:
    """Enrol an accepted paper so later work can cite it.

    Only accepted papers: a rejected or in-review paper is not something
    the lab should be citing as established. Returns None if the paper
    isn't accepted, so callers can fire this unconditionally.
    """
    paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    if paper is None or paper["status"] != "accepted":
        return None

    existing = conn.execute(
        "SELECT id FROM reference_works WHERE paper_id = ?", (paper_id,)
    ).fetchone()
    if existing:
        return existing["id"]

    # Authors: the byline if this was a collaboration, else the sole author.
    authors = [
        r["student_id"]
        for r in conn.execute(
            "SELECT student_id FROM paper_authors WHERE paper_id = ? ORDER BY author_order",
            (paper_id,),
        )
    ] or [paper["student_id"]]

    return add_reference(
        conn,
        title=paper["title"],
        authors=", ".join(f"Student {sid}" for sid in authors),
        identifier=f"autoprof:paper/{paper_id}",
        venue="auto-prof Lab",
        kind="internal_paper",
        # Internal papers are verified by construction: they exist, they
        # passed independent review, and their metadata comes from our own
        # rows rather than from a model's recollection.
        status=VERIFIED,
        paper_id=paper_id,
    )


def cite(conn: sqlite3.Connection, paper_id: int, reference_id: int) -> None:
    """Record that `paper_id` cites `reference_id`."""
    conn.execute(
        "INSERT OR IGNORE INTO reference_citations (paper_id, reference_id) VALUES (?, ?)",
        (paper_id, reference_id),
    )
    conn.commit()


def contaminated_papers(conn: sqlite3.Connection, reference_id: int):
    """Every paper that cited this reference -- who to revisit when it
    turns out to be wrong."""
    return conn.execute(
        "SELECT papers.* FROM papers JOIN reference_citations ON reference_citations.paper_id = papers.id "
        "WHERE reference_citations.reference_id = ? ORDER BY papers.id",
        (reference_id,),
    ).fetchall()


def citable(conn: sqlite3.Connection, limit: int = 100):
    """Verified works only -- what a student is allowed to cite."""
    return conn.execute(
        "SELECT * FROM reference_works WHERE status = 'verified' "
        "ORDER BY kind, year DESC, id LIMIT ?",
        (limit,),
    ).fetchall()


def render_for_prompt(conn: sqlite3.Connection, limit: int = 100) -> str:
    """The bank as a student sees it when writing up.

    States the rule as well as the list, because the list alone does not
    stop invention -- the failure being prevented is a model producing a
    confident reference that does not exist.
    """
    rows = citable(conn, limit)
    if not rows:
        return (
            "The lab's reference bank is empty. Do NOT invent references. If your argument "
            "needs prior work you cannot verify, state it as an assumption inherited from the "
            "problem statement and say so explicitly."
        )

    external, internal = [], []
    for row in rows:
        bits = [f"[{row['id']}] {row['authors']}. {row['title']}."]
        if row["venue"]:
            bits.append(f"{row['venue']}.")
        if row["year"]:
            bits.append(f"{row['year']}.")
        if row["identifier"]:
            bits.append(row["identifier"])
        (internal if row["kind"] == "internal_paper" else external).append(" ".join(bits))

    sections = []
    if external:
        sections.append(
            "PUBLISHED PRIOR WORK -- verified against the real record. Cite these normally:\n"
            "<published_works>\n" + "\n".join(external) + "\n</published_works>"
        )
    if internal:
        sections.append(
            "INTERNAL LAB RESULTS -- papers this lab produced and accepted through its own peer "
            "review. They are real and checkable inside the lab, but they are NOT published "
            "literature and a reviewer cannot look them up externally:\n"
            "<internal_lab_results>\n" + "\n".join(internal) + "\n</internal_lab_results>"
        )

    return (
        "The lab's shared reference bank.\n\n" + "\n\n".join(sections) + "\n\n"
        "Citation rules, which reviewers check:\n"
        "- Cite published prior work from the bank normally, reproducing title, authors and "
        "venue exactly as given.\n"
        "- When you cite an INTERNAL lab result, label it as such in the reference entry -- "
        "e.g. 'auto-prof Lab internal report, paper #3 (not externally published)'. Do NOT "
        "present it as published literature: a reviewer who cannot identify a reference "
        "independently will treat it as fabricated, which sinks the paper.\n"
        "- Never claim priority or novelty on the basis of an internal result alone. An "
        "internal paper establishes what THIS lab has shown; it says nothing about what the "
        "wider literature already contains.\n"
        "- You may cite a work NOT in the bank only if you are certain it exists and the "
        "metadata is right. Never invent a plausible-looking reference: a fabricated citation "
        "is treated as a correctness failure, not a formatting slip.\n"
        "- If you cannot verify a work your argument leans on, drop the claim or state it as an "
        "explicit assumption rather than attributing it to a source."
    )


SEED_PROMPT_TEMPLATE = """You are helping stock a research lab's reference bank at the moment \
the lab is founded.

The lab's field: {field}

The lab's root problem:
<root_problem>
{root_problem}
</root_problem>

List the prior works a researcher on this problem would be expected to know and cite: the \
classical results the problem builds on, the standard references for its definitions, and any \
recent work that bears directly on it.

Accuracy matters far more than quantity here. These entries will be checked against the real \
record, and anything you invent will be found and marked disputed.

- Only list works you are confident actually exist.
- Give the title EXACTLY as published -- not a paraphrase, not a descriptive title you have \
reconstructed from the content.
- If you are unsure of a work's exact title, venue or year, still include it but put what you \
are unsure about in "uncertain".
- Prefer fewer, certain entries over a long speculative list. An empty list is a valid answer \
if you genuinely know of no specific prior work.

Respond with ONLY a JSON object, no markdown fences, no commentary, in exactly this shape:
{{"works": [{{"title": "...", "authors": "...", "venue": "...", "year": 1978, \
"identifier": "arXiv:... or doi:... or null", "uncertain": "..."}}]}}
"""

VERIFY_PROMPT_TEMPLATE = """You are verifying bibliographic entries for a research lab's \
reference bank. Each was proposed by a model and has NOT been checked.

For each entry below, determine whether the work actually exists and whether the metadata is \
correct. Look it up rather than relying on recollection where you can.

<entries>
{entries}
</entries>

For each entry return a verdict:
- "verified": the work exists and title, authors, venue and year are correct as given. If they \
are nearly right, return "verified" WITH the corrected fields -- a wrong title on a real work \
is a correction, not a rejection.
- "disputed": you cannot confirm this work exists, or the entry conflates several works, or the \
title appears to be invented. Say why.

Be strict. A fabricated reference that reaches a paper is treated as a correctness failure, so \
"I think something like this exists" is a dispute, not a verification.

Respond with ONLY a JSON object, no markdown fences, no commentary:
{{"results": [{{"id": 3, "verdict": "verified|disputed", "title": "corrected title", \
"authors": "corrected authors", "venue": "...", "year": 1978, "note": "..."}}]}}
Include "id" exactly as given. Include corrected fields only when they differ.
"""


def seed_from_root_problem(conn, backend, root_problem: str, field: str) -> list[int]:
    """Propose prior art for a new lab, stored UNVERIFIED.

    Deliberately not citable yet. A model asked for references will
    produce confident, plausible, non-existent ones -- the exact failure
    this bank exists to prevent -- so seeding contributes candidates and
    verification decides what a student may actually cite.

    Returns the ids created. Never raises: a lab that cannot be seeded is
    still a perfectly good lab with an empty bank.
    """
    from .jsonio import extract_json_object

    result = backend.run(
        SEED_PROMPT_TEMPLATE.format(root_problem=root_problem.strip(), field=field.strip())
    )
    if result.is_error or result.rate_limited or not result.text.strip():
        return []

    try:
        payload = extract_json_object(result.text)
        works = payload.get("works") or []
    except Exception:  # noqa: BLE001 -- seeding is best-effort by design
        return []

    created = []
    for work in works:
        if not isinstance(work, dict):
            continue
        title = str(work.get("title") or "").strip()
        authors = str(work.get("authors") or "").strip()
        if not title or not authors:
            continue
        year = work.get("year")
        try:
            year = int(year) if year is not None else None
        except (TypeError, ValueError):
            year = None
        identifier = work.get("identifier")
        identifier = str(identifier).strip() if identifier else None
        if identifier and identifier.lower() in ("null", "none", ""):
            identifier = None
        try:
            created.append(
                add_reference(
                    conn,
                    title=title,
                    authors=authors,
                    identifier=identifier,
                    venue=(str(work["venue"]).strip() if work.get("venue") else None),
                    year=year,
                    status=UNVERIFIED,
                    notes=(str(work["uncertain"]).strip() if work.get("uncertain") else None),
                )
            )
        except ReferenceError:
            continue
    return created


def pending_verification(conn, limit: int = 25):
    return conn.execute(
        "SELECT * FROM reference_works WHERE status = 'unverified' ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()


def render_entries_for_verification(rows) -> str:
    lines = []
    for row in rows:
        bits = [f"id={row['id']}", f"title={row['title']!r}", f"authors={row['authors']!r}"]
        if row["venue"]:
            bits.append(f"venue={row['venue']!r}")
        if row["year"]:
            bits.append(f"year={row['year']}")
        if row["identifier"]:
            bits.append(f"identifier={row['identifier']!r}")
        if row["notes"]:
            bits.append(f"proposer_uncertain_about={row['notes']!r}")
        lines.append("  " + ", ".join(bits))
    return "\n".join(lines)


def apply_verification(conn, results) -> tuple[int, int]:
    """Apply verdicts, correcting metadata where the checker supplied it.

    A near-miss on a real work is a correction, not a rejection -- that is
    the case that actually occurred (a real paper carrying a fabricated
    title), and dropping it would lose a legitimate reference.
    """
    verified = disputed = 0
    for item in results or []:
        if not isinstance(item, dict) or "id" not in item:
            continue
        try:
            ref_id = int(item["id"])
        except (TypeError, ValueError):
            continue
        row = conn.execute("SELECT * FROM reference_works WHERE id = ?", (ref_id,)).fetchone()
        if row is None:
            continue

        verdict = str(item.get("verdict", "")).strip().lower()
        if verdict == "verified":
            for column in ("title", "authors", "venue"):
                value = item.get(column)
                if value and str(value).strip() and str(value).strip() != row[column]:
                    conn.execute(
                        f"UPDATE reference_works SET {column} = ? WHERE id = ?",
                        (str(value).strip(), ref_id),
                    )
            year = item.get("year")
            if year:
                try:
                    conn.execute(
                        "UPDATE reference_works SET year = ? WHERE id = ?", (int(year), ref_id)
                    )
                except (TypeError, ValueError):
                    pass
            set_status(conn, ref_id, VERIFIED, item.get("note"))
            verified += 1
        elif verdict == "disputed":
            set_status(conn, ref_id, DISPUTED, item.get("note"))
            disputed += 1
    conn.commit()
    return verified, disputed


def execute_reference_verify_job(conn, job_id: int, backend, lab_dir) -> str:
    """Check the bank's unverified entries against the real record.

    Runs as its own job so lab creation does not block on it, and so a
    failed verification is retried by the normal job machinery rather than
    leaving a lab half-seeded. Entries stay unverified -- and therefore
    uncitable -- until this succeeds, so a failure is safe: the worst case
    is a student writing with an empty bank and being told not to invent.
    """
    import uuid

    from . import jobs
    from .jsonio import extract_json_object

    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=1800):
        return "not_claimed"

    job_row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    rows = pending_verification(conn)
    if not rows:
        jobs.complete_job(conn, job_id, lease_id)
        return "done"

    result = jobs.run_with_session(
        conn,
        job_id,
        backend,
        VERIFY_PROMPT_TEMPLATE.format(entries=render_entries_for_verification(rows)),
    )

    if result.rate_limited:
        jobs.record_rate_limit(
            conn, job_id, lease_id, result.retry_after_seconds, provider=backend.name
        )
        return "rate_limited"
    if result.is_error:
        return jobs.fail_job(conn, job_id, lease_id, result.error)

    try:
        payload = extract_json_object(result.text)
    except Exception as e:  # noqa: BLE001
        return jobs.fail_job(
            conn, job_id, lease_id, f"unusable verification output: {e} -- raw: {result.text[:300]}"
        )

    verified, disputed = apply_verification(conn, payload.get("results"))

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"

    from .events import record_job_event

    record_job_event(
        conn,
        job_id=job_id,
        actor_type="daemon",
        actor_id=None,
        event_type="references_verified",
        target_type="lab",
        target_id=job_row["target_id"],
    )
    conn.commit()
    return "done"
