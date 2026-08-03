"""First-principles discipline, made mechanical.

"Think from first principles" is unenforceable as advice. What IS
enforceable is the practice underneath it:

1. **Separate inherited from derived.** A student handed a brief inherits
   its framing, its definitions and its conjecture. Some of that is sound
   and some is the professor's guess. Recording which is which is the
   difference between building on a result and building on a hunch.
2. **Make load-bearing premises explicit.** A premise nobody wrote down
   cannot be challenged, and a reviewer will find it eventually -- three
   papers in this system carried a fabricated citation because nobody
   questioned an inherited reference.
3. **Check what is checkable.** Students have a verifier. A finite
   assumption that stays `assumed` when it could have been `verified` is
   a choice, not a limitation.

The ledger also gives refutation a blast radius: when an assumption turns
out false, `dependents` names every task and paper standing on it instead
of leaving someone to grep -- the selective traceback the resilience
design asks for, applied to premises.
"""

import re
import sqlite3

SOURCES = ("root_problem", "brief", "prior_paper", "derived", "inherited")
STATUSES = ("assumed", "derived", "verified", "refuted")

_BLOCK_RE = re.compile(r"```assume\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)

ASSUMPTION_DOCS = """**Work from first principles.** Before extending anything, derive the \
central objects from their definitions rather than from how your brief describes them. Your brief \
is your professor's best guess at a framing, not established fact -- more than one conjecture in \
this lab has turned out false, and the student who checked found it.

Register what your work stands on. Emit blocks like this anywhere in your response:

```assume
statement: every pure rank-r system with defect below 1/2 is a matroid
source: derived
status: derived
evidence: proved in section 3 by minimal-distance failed exchange
```

- `source`: root_problem | brief | prior_paper | derived | inherited
- `status`: assumed | derived | verified | refuted
- Use `inherited` honestly. An assumption you took from your brief without re-deriving is \
`inherited`/`assumed`, not `derived`, however confident you are.
- If an assumption is FINITE, check it with the verify tool and mark it `verified`, citing the \
run. An assumption you could have checked and didn't is a choice.
- If you find an inherited assumption is FALSE, mark it `refuted` and say so plainly. That is a \
result, often a better one than what you were asked for.
"""


class AssumptionError(RuntimeError):
    pass


def parse_blocks(text: str) -> list[dict]:
    """Pull ```assume``` blocks out of a model response.

    Tolerant by design: a malformed block is skipped rather than failing
    the job. Losing one ledger entry is a small cost; losing a completed
    research pass because a colon was missing is not.
    """
    found = []
    for match in _BLOCK_RE.finditer(text or ""):
        entry = {}
        for line in match.group(1).splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            if key in ("statement", "source", "status", "evidence"):
                entry[key] = value.strip()
        statement = entry.get("statement", "").strip()
        if not statement:
            continue
        source = entry.get("source", "inherited").strip().lower()
        status = entry.get("status", "assumed").strip().lower()
        found.append({
            "statement": statement,
            "source": source if source in SOURCES else "inherited",
            "status": status if status in STATUSES else "assumed",
            "evidence": entry.get("evidence", "").strip() or None,
        })
    return found


def record(conn: sqlite3.Connection, entries, *, lab_id: int, task_id, student_id) -> list[int]:
    """Upsert ledger entries by statement within a task.

    Matched on the statement text so a student revisiting an assumption
    across rounds UPDATES it -- `assumed` becoming `verified` or `refuted`
    is the whole point -- rather than accumulating near-duplicates that
    make the ledger unreadable.
    """
    ids = []
    for entry in entries:
        existing = conn.execute(
            "SELECT id FROM assumptions WHERE task_id IS ? AND statement = ?",
            (task_id, entry["statement"]),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE assumptions SET status = ?, source = ?, "
                "evidence = COALESCE(?, evidence), updated_at = datetime('now') WHERE id = ?",
                (entry["status"], entry["source"], entry["evidence"], existing["id"]),
            )
            ids.append(existing["id"])
        else:
            cur = conn.execute(
                "INSERT INTO assumptions (lab_id, task_id, student_id, statement, source, "
                "status, evidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (lab_id, task_id, student_id, entry["statement"], entry["source"],
                 entry["status"], entry["evidence"]),
            )
            ids.append(cur.lastrowid)
    conn.commit()
    return ids


def ledger(conn: sqlite3.Connection, task_id: int):
    return conn.execute(
        "SELECT * FROM assumptions WHERE task_id = ? "
        "ORDER BY CASE status WHEN 'refuted' THEN 0 WHEN 'assumed' THEN 1 ELSE 2 END, id",
        (task_id,),
    ).fetchall()


def dependents(conn: sqlite3.Connection, assumption_id: int):
    """What stands on this assumption -- the blast radius of refuting it."""
    row = conn.execute("SELECT * FROM assumptions WHERE id = ?", (assumption_id,)).fetchone()
    if row is None:
        return {"tasks": [], "papers": []}
    tasks = conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (row["task_id"],)
    ).fetchall() if row["task_id"] else []
    papers = conn.execute(
        "SELECT * FROM papers WHERE task_id = ? ORDER BY id", (row["task_id"],)
    ).fetchall() if row["task_id"] else []
    return {"tasks": tasks, "papers": papers}


def render(conn: sqlite3.Connection, task_id: int, *, for_professor: bool = False) -> str:
    """The ledger as an agent sees it.

    The professor's view leads with the challenge, because the failure
    being prevented is a supervisor who accepts the student's framing as
    readily as the student did.
    """
    rows = ledger(conn, task_id)
    if not rows:
        if for_professor:
            return (
                "Your student has registered NO assumptions. That is itself worth challenging: "
                "every piece of work stands on something. Ask what they are taking from the "
                "brief without having re-derived it."
            )
        return "You have not registered any assumptions yet."

    lines = []
    for row in rows:
        bit = f"- [{row['id']}] ({row['source']}/{row['status']}) {row['statement']}"
        if row["evidence"]:
            bit += f"\n      evidence: {row['evidence']}"
        lines.append(bit)
    body = "<assumption_ledger>\n" + "\n".join(lines) + "\n</assumption_ledger>"

    unexamined = [r for r in rows if r["status"] == "assumed"]
    refuted = [r for r in rows if r["status"] == "refuted"]

    if for_professor:
        head = (
            "What your student's work currently stands on. Examine it: an unexamined inherited "
            "assumption is the most common way this lab's papers have failed review."
        )
        tail = []
        if unexamined:
            tail.append(
                f"{len(unexamined)} assumption(s) are still merely ASSUMED. Challenge at least "
                "one of them by name in your guidance -- ask them to derive it, check it with "
                "the verifier, or drop the claim that needs it."
            )
        if refuted:
            tail.append(
                f"{len(refuted)} assumption(s) are REFUTED. Anything still relying on them is "
                "unsound and must be revised or withdrawn."
            )
        return "\n\n".join([head, body, *tail])

    head = "Assumptions you have registered for this task:"
    tail = []
    if unexamined:
        tail.append(
            "The ones marked `assumed` are unexamined. For each, either derive it, verify it "
            "computationally if it is finite, or state plainly in your write-up that your result "
            "is conditional on it."
        )
    if refuted:
        tail.append(
            "You have REFUTED an assumption. Make sure nothing in your current argument still "
            "depends on it."
        )
    return "\n\n".join([head, body, *tail])
