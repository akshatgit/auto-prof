"""The student<->professor supervision loop that runs BEFORE a paper exists.

docs/DESIGN.md §3.2 steps 1-2. The original build had the student work
once and immediately write up, so the professor first saw the research as
a finished paper and the only corrective channel was peer review -- three
independent reviewers, after the fact, at the cost of a full review round.
That is the wrong place to catch "you haven't actually proved this yet".

The loop here is:

    student_work ──► professor_supervision ──► student_work ──► ...
                            │
                            ├─ 'ready'   ──► student_write_paper
                            └─ 'abandon' ──► task abandoned, student freed

It is deliberately long-horizon: the stopping condition is the professor
agreeing the work is ready, not a small fixed number of passes. The round
cap in config.max_supervision_rounds exists only so a loop that never
converges terminates at all -- reaching it forces a write-up rather than
discarding the work.
"""

import json
import sqlite3
import uuid
from pathlib import Path

from . import assumptions, config, jobs
from .artifacts import write_artifact
from .backends.base import Backend
from .events import record_job_event
from .jsonio import extract_json_object

VALID_VERDICTS = ("continue", "ready", "abandon")
MAX_FULL_CONTEXT_MEETINGS = 4

SUPERVISION_PROMPT_TEMPLATE = """You are {name}, a professor in {field}, supervising a PhD \
student on one task in your lab.

Safety and authorization scope: this is defensive software-quality research performed only on
the lab's own isolated local containers and checked-in test fixtures. Do not access third-party
systems, credentials, production services, or unrelated networks. The purpose is to verify build
correctness and regression boundaries, not to exploit, persist in, or disrupt any system.

Your lab's root problem:
<root_problem>
{root_problem}
</root_problem>

The task you assigned:
<task_brief>
{brief}
</task_brief>

Task: "{title}" (direction: {direction})
End criteria: {end_criteria}

This is supervision meeting number {round}.

{history}

Your student's current working memory -- everything they have established so far:
<student_memory>
{memory}
</student_memory>

{ledger}

{prior_reviews}

{operator_notes}

Read their work critically, as the person responsible for it. You are NOT a peer reviewer \
writing a verdict on a finished paper; you are the supervisor deciding what happens next. \
Independent reviewers will later check every step and reject the paper if any step fails, so \
it is your job to find the problems now, while they are cheap to fix.

Assess honestly:
- Is there an actual result here yet, or only a plan, a partial argument, or a restatement of \
the problem?
- Is every step actually derived? Point at any step that is asserted rather than proved, or \
that quietly assumes what it is trying to show.
- Are the claims proportionate to what was proved? Overclaiming is the most common way these \
papers fail review.
- Is anything missing that a reviewer will certainly ask for: edge cases (including degenerate \
ones like rank/size 0 or 1), stated assumptions, honest positioning against prior work?
- What is the work standing on that nobody has checked? Look at the assumption ledger above and \
challenge at least one entry by name. Do not accept your own brief's framing just because you \
wrote it -- a conjecture you set them is exactly the kind of thing that turns out false, and the \
student who tests it has done better work than the one who assumes it.
- Is the result significant enough to be worth writing up, or should the student push further \
first? A narrow-but-correct result that reviewers call "elementary" is a real failure mode.

Then decide one of:
- "continue": the student should keep working. You MUST give specific, actionable guidance -- \
name the exact gap, the exact step to fix, or the exact extension to attempt. Vague \
encouragement is useless and wastes a round.
- "ready": the work is genuinely ready to be written up as a paper that could survive \
independent review. Do not say this merely because progress has been made.
- "abandon": this line of attack is not going to work, and the honest move is to stop. Say why.

A `ready` decision is machine-gated. Set `end_criteria_met` true only when the task's stated
success criteria OR its explicitly stated negative-close criteria are all satisfied by artifacts
that exist now. List those artifacts/results in `completion_evidence`, and list every remaining
gap in `remaining_gaps`. If any required criterion remains unmet, the verdict must be `continue`
or `abandon`; changing venue or calling an incomplete result a workshop paper does not satisfy
the gate.

Respond with ONLY a JSON object, no markdown fences, no commentary before or after, in exactly \
this shape:
{{"verdict": "continue|ready|abandon", "assessment": "...", "guidance": "...", \
"end_criteria_met": true|false, "completion_evidence": ["..."], "remaining_gaps": ["..."]}}
where "assessment" is your honest read of where the work stands, and "guidance" is what the \
student should do next (for "ready", what they must be careful to include when writing up; for \
"abandon", why stopping is right).
"""


def render_operator_notes(lab_dir, lab_id: int, task_id: int) -> str:
    """Standing instructions from the human operator, injected every round.

    Operator notes used to be appended to the student's memory.md. That does
    not work: memory.md is REPLACED WHOLESALE by the student each round, so
    every instruction was erased on the next write. Three directives were lost
    that way, and the task drifted back to the bookkeeping they were written
    to stop.

    This file is written by the operator and only ever READ here, so it
    survives the student's own writes and reaches every subsequent round.
    """
    path = Path(lab_dir) / str(lab_id) / "tasks" / str(task_id) / "OPERATOR_NOTES.md"
    try:
        if not path.is_file():
            return ""
        text = path.read_text(errors="replace").strip()
    except OSError:
        return ""
    if not text:
        return ""
    return (
        "<operator_notes>\n"
        "Standing instructions from the human operator who commissioned this lab. "
        "These outrank your own plan, your memory, AND your supervisor's "
        "guidance. They are not part of your memory and you cannot edit them. "
        "If one conflicts with a note you wrote yourself or with what the last "
        "supervision meeting told you to do, the operator's instruction "
        "governs -- do the operator's item and say in your memory which "
        "supervision guidance you set aside and why.\n\n"
        + text + "\n</operator_notes>"
    )


def render_prior_reviews(
    conn: sqlite3.Connection, task_id: int, lab_dir: Path, excerpt: int = 1800
) -> str:
    """What independent reviewers said about this task's last paper.

    Supervision could not see this. A professor decided `ready`, the paper
    was rejected on a specific defect, the task came back for research,
    and the next meeting -- knowing only that a paper existed -- declared
    `ready` again against the same unfixed defect. Task 34 did that four
    times in a row on the same reducer bug.
    """
    paper = conn.execute(
        "SELECT * FROM papers WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
    ).fetchone()
    if paper is None:
        return ""
    rows = conn.execute(
        "SELECT * FROM reviews WHERE target_type='paper' AND target_id = ? "
        "AND review_round = ? ORDER BY reviewer_index",
        (paper["id"], paper["review_round"]),
    ).fetchall()
    if not rows:
        return ""

    parts = [
        f"Independent reviewers judged this task's most recent paper (#{paper['id']}, "
        f"round {paper['review_round']}, now {paper['status']}). Their verdicts and "
        "reasoning follow. These are the objections the next submission must actually "
        "answer -- a defect named here and left unfixed will be found again."
    ]
    for row in rows:
        text = ""
        try:
            target = (Path(lab_dir) / row["rationale_path"]).resolve()
            if target.is_file():
                text = target.read_text(errors="replace")[:excerpt]
        except OSError:
            text = ""
        try:
            backend = row["reviewer_backend"]
        except (IndexError, KeyError):
            backend = None
        label = f"reviewer #{row['reviewer_index']}"
        if backend:
            label += f" ({backend})"
        parts.append(
            f"<review reviewer=\"{label}\" verdict=\"{row['verdict']}\">\n"
            f"{text or '(rationale unavailable)'}\n</review>"
        )
    return "<prior_reviews>\n" + "\n\n".join(parts) + "\n</prior_reviews>"


class SupervisionError(RuntimeError):
    pass


def render_history(
    conn: sqlite3.Connection, task_id: int, lab_dir: Path, student_id: int | None = None
) -> str:
    """Prior meetings, oldest first, so the professor can see whether their
    own guidance was actually followed.

    Without this each meeting would be memoryless and the professor could
    ask for the same fix indefinitely -- the exact failure the loop exists
    to avoid.
    """
    all_rows = conn.execute(
        "SELECT * FROM supervisions WHERE task_id = ? ORDER BY round", (task_id,)
    ).fetchall()

    # Meetings held with a DIFFERENT student are somebody else's record, not
    # guidance this student failed to follow. A reopened task keeps counting
    # rounds (`round` is UNIQUE per task and names the artifact file), so a
    # replacement student's first meeting was numbered 54 and arrived with 53
    # rounds of their predecessor's history -- including the abandon that
    # freed the task. The professor read that as its own exhausted patience
    # and abandoned again on the new student's first round. Scope the
    # relationship to this student and summarise the rest as inherited
    # context.
    prior = [r for r in all_rows if student_id is not None and r["student_id"] != student_id]
    rows = [r for r in all_rows if student_id is None or r["student_id"] == student_id]

    preamble = ""
    if prior:
        outcomes = ", ".join(f"{r['round']}:{r['verdict']}" for r in prior[-12:])
        preamble = (
            f"A previous student worked this task for {len(prior)} meetings before being "
            f"replaced; their memory was handed over to the current student. Recent outcomes: "
            f"{outcomes}. That record is context, NOT guidance this student ignored -- do not "
            "hold it against them, and judge this student on their own meetings below.\n\n"
        )

    if not rows:
        return preamble + (
            "This is your first meeting with this student; there is no prior guidance to them."
        )

    omitted = rows[:-MAX_FULL_CONTEXT_MEETINGS]
    included = rows[-MAX_FULL_CONTEXT_MEETINGS:]
    parts = []
    if omitted:
        outcomes = ", ".join(f"{row['round']}:{row['verdict']}" for row in omitted)
        parts.append(
            f"{len(omitted)} older meetings were compacted to prevent context growth. "
            f"Outcome index: {outcomes}. The student's current memory is authoritative for "
            "work carried forward from them."
        )
    for row in included:
        path = lab_dir / row["guidance_path"]
        body = path.read_text(errors="replace") if path.exists() else "(guidance file missing)"
        parts.append(f"--- Meeting {row['round']} (you said: {row['verdict']}) ---\n{body}")
    return (
        preamble
        + "Your own guidance from previous meetings with THIS student, oldest first. "
        "Check whether the student actually acted on it:\n<supervision_history>\n"
        + "\n\n".join(parts)
        + "\n</supervision_history>"
    )


def _next_round(conn: sqlite3.Connection, task_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(round), 0) AS r FROM supervisions WHERE task_id = ?", (task_id,)
    ).fetchone()
    return row["r"] + 1


def _current_attempt(conn: sqlite3.Connection, task_id: int) -> int:
    """Which paper attempt this task is on. Writing a paper ends an
    attempt, so the count of papers written so far identifies the next."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM papers WHERE task_id = ?", (task_id,)
    ).fetchone()
    return row["n"] + 1


def _round_within_attempt(conn: sqlite3.Connection, task_id: int, attempt: int) -> int:
    """Which supervision meeting this is *for the current attempt*.

    The cap is measured against this rather than against `round`, which is
    cumulative and names the artifact file, so it can never reset.

    Once `round` passed the cap on task #4 the condition was permanently
    true, so every later meeting was force-resolved to 'ready' -- 28 of
    them -- and the professor could never say 'continue' again. The
    student stopped doing research and only re-drafted the same unproven
    theorem. Counting per attempt restores what the cap was protecting: a
    fresh budget of real supervision behind each new attempt.

    Rows written before the `attempt` column existed have NULL and are all
    treated as attempt 1.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM supervisions WHERE task_id = ? "
        "AND COALESCE(attempt, 1) = ?",
        (task_id, attempt),
    ).fetchone()
    return row["n"] + 1


def execute_professor_supervision_job(
    conn: sqlite3.Connection, job_id: int, backend: Backend, lab_dir: Path
) -> str:
    """Daemon special_handlers signature: (conn, job_id, backend, lab_dir)."""
    lease_id = uuid.uuid4().hex
    if not jobs.claim_job(conn, job_id, lease_id, lease_seconds=3600):
        return "not_claimed"

    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    task = conn.execute("SELECT * FROM tasks WHERE id = ?", (row["target_id"],)).fetchone()
    if task is None:
        return jobs.fail_job(conn, job_id, lease_id, f"no task with id={row['target_id']}")
    if task["assigned_student_id"] is None:
        return jobs.fail_job(conn, job_id, lease_id, f"task {task['id']} has no assigned student")

    student = conn.execute(
        "SELECT * FROM students WHERE id = ?", (task["assigned_student_id"],)
    ).fetchone()
    lab = conn.execute("SELECT * FROM labs WHERE id = ?", (task["lab_id"],)).fetchone()
    professor = conn.execute(
        "SELECT * FROM professors WHERE id = ?", (student["professor_id"],)
    ).fetchone()

    memory_file = lab_dir / student["memory_path"]
    memory = memory_file.read_text(errors="replace") if memory_file.exists() else "(no memory recorded yet)"
    brief_file = lab_dir / task["brief_path"]
    brief = brief_file.read_text(errors="replace") if brief_file.exists() else "(no brief written)"

    round_ = _next_round(conn, task["id"])

    result = jobs.run_with_session(
        conn,
        job_id,
        backend,
        SUPERVISION_PROMPT_TEMPLATE.format(
            name=professor["name"],
            field=professor["field"],
            root_problem=lab["root_problem"],
            brief=brief,
            title=task["title"],
            direction=task["direction"],
            end_criteria=task["end_criteria"],
            round=round_,
            history=render_history(conn, task["id"], lab_dir, student["id"]),
            memory=memory,
            ledger=assumptions.render(conn, task['id'], for_professor=True),
            prior_reviews=render_prior_reviews(conn, task["id"], lab_dir),
            operator_notes=render_operator_notes(lab_dir, lab["id"], task["id"]),
        ),
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
        verdict = str(payload["verdict"]).strip().lower()
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        return jobs.fail_job(
            conn, job_id, lease_id, f"unusable supervision output: {e} -- raw: {result.text[:300]}"
        )
    if verdict not in VALID_VERDICTS:
        return jobs.fail_job(
            conn, job_id, lease_id, f"supervision verdict {verdict!r} not one of {VALID_VERDICTS}"
        )

    assessment = str(payload.get("assessment", "")).strip()
    guidance = str(payload.get("guidance", "")).strip()
    end_criteria_met = payload.get("end_criteria_met") is True
    completion_evidence = payload.get("completion_evidence")
    remaining_gaps = payload.get("remaining_gaps")
    evidence_ok = (
        isinstance(completion_evidence, list)
        and any(str(item).strip() for item in completion_evidence)
    )
    gaps_ok = isinstance(remaining_gaps, list) and not remaining_gaps
    if verdict == "ready" and not (end_criteria_met and evidence_ok and gaps_ok):
        verdict = "continue"
        gate_reason = (
            "Readiness gate refused the requested ready verdict: all end criteria were not "
            "affirmed with concrete completion evidence and an empty remaining-gaps list."
        )
        guidance = f"{gate_reason} {guidance}".strip()

    # A paper that was just rejected cannot be re-declared ready without a
    # single round of research in between. Task 34 alternated ready ->
    # write -> reject -> ready nine times in three hours, resubmitting
    # against reviewer objections nobody had gone back to fix, because
    # nothing required a research round between an attempt and the next
    # one. This does not make any paper easier to accept; it only stops
    # the same paper being thrown at the panel again unchanged.
    if verdict == "ready":
        last_paper = conn.execute(
            "SELECT id, status, created_at FROM papers WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (task["id"],),
        ).fetchone()
        if last_paper is not None and last_paper["status"] == "rejected":
            research_since = conn.execute(
                "SELECT COUNT(*) AS n FROM supervisions WHERE task_id = ? "
                "AND verdict = 'continue' AND created_at > ?",
                (task["id"], last_paper["created_at"]),
            ).fetchone()["n"]
            if not research_since:
                verdict = "continue"
                guidance = (
                    "Paper #%d was rejected and no research round has happened since. "
                    "Resubmitting now sends the reviewers the same document against the "
                    "same objections. Address what they actually named, then say ready. "
                    "%s" % (last_paper["id"], guidance)
                ).strip()

    max_rounds = config.max_supervision_rounds(lab_id=lab["id"])
    attempt = _current_attempt(conn, task["id"])
    forced = False
    if (
        verdict == "continue"
        and max_rounds
        and _round_within_attempt(conn, task["id"], attempt) >= max_rounds
    ):
        # A round ceiling is not evidence that the work is publishable.
        # The old behaviour forced incomplete work into review and paper 55
        # reached that gate with only ~27% of its end criteria complete.
        verdict = "abandon"
        forced = True

    relpath = f"{lab['id']}/tasks/{task['id']}/supervision/{round_}.md"
    write_artifact(
        lab_dir / relpath,
        f"# Supervision meeting {round_} -- verdict: {verdict}"
        + (f" (abandoned at round cap {max_rounds}; not forced ready)" if forced else "")
        + f"\n\n## Assessment\n\n{assessment or '(none given)'}"
        f"\n\n## Guidance\n\n{guidance or '(none given)'}\n",
    )

    conn.execute(
        "INSERT INTO supervisions (task_id, student_id, round, verdict, guidance_path, attempt) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (task["id"], student["id"], round_, verdict, relpath, attempt),
    )

    if verdict == "continue":
        conn.execute("UPDATE students SET status = 'working' WHERE id = ?", (student["id"],))
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('student_work', 'task', ?, 'pending')",
            (task["id"],),
        )
    elif verdict == "ready":
        conn.execute("UPDATE students SET status = 'writing_paper' WHERE id = ?", (student["id"],))
        conn.execute(
            "INSERT INTO jobs (kind, target_type, target_id, status) "
            "VALUES ('student_write_paper', 'task', ?, 'pending')",
            (task["id"],),
        )
    else:  # abandon
        conn.execute("UPDATE tasks SET status = 'abandoned' WHERE id = ?", (task["id"],))
        # The schema's trg_tasks_release_student trigger frees the student
        # when a task is abandoned, so don't also write students here.

    if not jobs.complete_job(conn, job_id, lease_id, model_version=result.model_version):
        return "not_claimed"

    record_job_event(
        conn,
        job_id=job_id,
        actor_type="professor",
        actor_id=professor["id"],
        event_type=f"supervision_{verdict}",
        target_type="task",
        target_id=task["id"],
        payload_path=relpath,
    )
    conn.commit()
    return "done"


def render_student_guidance(conn: sqlite3.Connection, task_id: int, lab_dir: Path) -> str:
    """The supervisor's guidance, as the student should see it.

    Distinct from render_history: the professor needs the full record to
    judge whether their advice was followed, while the student needs the
    latest instruction foregrounded and the earlier ones as context. Both
    read the same rows -- this is a presentation difference, and getting it
    wrong (burying the current instruction in a wall of history) is how
    guidance gets ignored.
    """
    rows = conn.execute(
        "SELECT * FROM supervisions WHERE task_id = ? ORDER BY round", (task_id,)
    ).fetchall()
    if not rows:
        return "You have not met with your supervisor yet on this task."

    def body(row):
        path = lab_dir / row["guidance_path"]
        return path.read_text(errors="replace") if path.exists() else "(guidance file missing)"

    latest = rows[-1]
    out = [
        "Your supervisor has read your work. Their most recent guidance is below and you are "
        "expected to act on it:",
        f"<supervisor_guidance round=\"{latest['round']}\">\n{body(latest)}\n</supervisor_guidance>",
    ]
    if len(rows) > 1:
        included_earlier = rows[-MAX_FULL_CONTEXT_MEETINGS:-1]
        omitted = rows[:-MAX_FULL_CONTEXT_MEETINGS]
        earlier = "\n\n".join(
            f"--- Meeting {r['round']} ---\n{body(r)}" for r in included_earlier
        )
        if omitted:
            outcomes = ", ".join(f"{r['round']}:{r['verdict']}" for r in omitted)
            earlier = (
                f"{len(omitted)} older meetings compacted; outcome index: {outcomes}.\n\n"
                + earlier
            )
        out.append(
            "Earlier guidance on this task, for context (do not re-litigate points you have "
            f"already addressed):\n<earlier_guidance>\n{earlier}\n</earlier_guidance>"
        )
    return "\n\n".join(out)
