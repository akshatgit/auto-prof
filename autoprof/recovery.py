"""Deterministic failure classification and recovery.

The resilience design's §2/§17/§18, scoped to what auto-prof actually is:
one process, one SQLite file, no external side effects beyond atomic file
writes. Regional failover, content addressing and side-effect compensation
have no referent here and are deliberately absent.

The point of this module is that recovery is decided by a table, not by
whichever handler happened to catch the exception. Before it, every
handler hand-rolled its own failure branch, so "retry" meant different
things in different places and a deterministic failure was retried five
times at full cost -- observed live, when our own wall-clock timeout
killed a productive derivation three times over.

Two rules do most of the work:

- A failure is retried only if retrying could plausibly succeed. A
  malformed model response might parse next time; an invalid credential
  will not, and burning five attempts on it wastes an hour.
- A recovery is not complete because an action ran. It is complete when
  its postcondition is verified. An unverified "recovery" is how an empty
  backend response got recorded as a successful job.
"""

import re
import sqlite3
from dataclasses import dataclass, field

# --- classification ---------------------------------------------------
#
# Domains are the ones this system can actually produce. Each maps to a
# policy below; anything unrecognised classifies as UNKNOWN, which retries
# briefly and then escalates rather than guessing in either direction.

WORKER = "worker"                 # crash, timeout, lost lease
MODEL_OUTPUT = "model_output"     # malformed/unparseable/empty response
MODEL_CAPACITY = "model_capacity"  # rate limit, token/context exhaustion
TASK_LOGIC = "task_logic"         # the work itself is wrong or impossible
STATE_CONFLICT = "state_conflict"  # stale round, vanished row, lease lost
CONFIG = "config"                 # missing binary, bad credentials, bad path
UNKNOWN = "unknown"

_PATTERNS = (
    (MODEL_CAPACITY, r"rate.?limit|usage limit|429|context (length|window)|token limit|out of tokens|quota"),
    (CONFIG, r"not found on path|no such file|permission denied|unauthor|invalid.{0,12}(key|credential)|not configured"),
    (MODEL_OUTPUT, r"no verdict line|unusable|not an html document|produced no output|empty .*output|expected json|unparse|missing required keys|verdict .* not one of"),
    # Provider-side 5xx is the canonical transient failure and had no
    # pattern, so it fell through to UNKNOWN -- two quick attempts, then
    # dead. A burst of Ollama 500s killed four of lab #6's five tasks
    # inside one outage window; the backend was healthy again minutes
    # later and nothing ever retried.
    (WORKER, r"timed out|timeout|killed|broken pipe|connection reset"
             r"|http 5\d\d|internal server error|service unavailable|bad gateway"
             r"|gateway time-?out|temporarily unavailable|overloaded"),
    (STATE_CONFLICT, r"no longer exists|is now on round|has no assigned student|missing:|no (task|paper|lab|professor) with id"),
    (TASK_LOGIC, r"has no reviews to revise|cannot be completed"),
)


def classify_failure(error: str | None) -> str:
    """Map an error string to a domain. Deterministic and side-effect free
    so it can be unit-tested against real error text."""
    if not error:
        return UNKNOWN
    text = error.lower()
    for domain, pattern in _PATTERNS:
        if re.search(pattern, text):
            return domain
    return UNKNOWN


# --- policy -----------------------------------------------------------


@dataclass(frozen=True)
class RecoveryPolicy:
    """What to do about one class of failure.

    `retry` is whether re-running the identical job could plausibly
    succeed. `max_attempts` caps that independently of the job's own
    `max_attempts`, so a class known to be hopeless stops early rather
    than exhausting the generic budget.
    """

    domain: str
    retry: bool
    max_attempts: int
    escalate: bool           # surface to a human rather than fail silently
    preventive_rule: str = ""
    verification: tuple = field(default_factory=tuple)


POLICIES = {
    # A crashed or timed-out worker is the canonical transient failure:
    # the lease expires, another worker picks the job up, and with session
    # resume it continues rather than restarting.
    WORKER: RecoveryPolicy(WORKER, retry=True, max_attempts=5, escalate=False,
                           verification=("job_not_running",)),
    # Malformed output is worth a couple of retries -- sampling varies --
    # but not five: if the prompt reliably produces unparseable output,
    # repetition will not fix it.
    MODEL_OUTPUT: RecoveryPolicy(MODEL_OUTPUT, retry=True, max_attempts=3, escalate=False,
                                 preventive_rule="Tighten the output contract in the prompt, "
                                                 "or validate and re-prompt once with the errors.",
                                 verification=("job_not_running",)),
    # Never counts as a failure at all -- handled by record_rate_limit,
    # which backs off without touching `attempts`. Listed so the classifier
    # has somewhere to put it and so it is never mistaken for an error.
    MODEL_CAPACITY: RecoveryPolicy(MODEL_CAPACITY, retry=True, max_attempts=99, escalate=False,
                                   verification=("job_not_running",)),
    # The state moved under this job (its round advanced, its target was
    # abandoned). Retrying the same job cannot help; the work must be
    # re-derived from current state.
    STATE_CONFLICT: RecoveryPolicy(STATE_CONFLICT, retry=False, max_attempts=1, escalate=False,
                                   preventive_rule="Re-read target state after claiming, and fail "
                                                   "fast when the round or assignment has moved."),
    # The task as posed cannot be completed. Retrying is pure waste; a
    # human or the professor must re-scope it.
    TASK_LOGIC: RecoveryPolicy(TASK_LOGIC, retry=False, max_attempts=1, escalate=True,
                               preventive_rule="Re-scope or abandon the task; identical retries "
                                               "cannot resolve a logic failure."),
    CONFIG: RecoveryPolicy(CONFIG, retry=False, max_attempts=1, escalate=True,
                           preventive_rule="Fix the environment (binary on PATH, credentials, "
                                           "paths) before re-running; no retry can."),
    # Unclassified: retry, but briefly, and escalate. Re-running a job is
    # a reversible internal action -- cheap, no external effect -- so the
    # design's "aggressive for reversible failures, conservative for
    # uncertain external effects" rule points at retrying. The low cap and
    # the escalation flag are what stop it from silently burning budget on
    # something nobody has diagnosed.
    UNKNOWN: RecoveryPolicy(UNKNOWN, retry=True, max_attempts=2, escalate=True,
                            preventive_rule="Unclassified failure -- add a pattern to "
                                            "recovery._PATTERNS once the cause is understood.",
                            verification=("job_not_running",)),
}


def lookup(classification: str) -> RecoveryPolicy:
    return POLICIES.get(classification, POLICIES[UNKNOWN])


def should_retry(classification: str, attempts: int) -> bool:
    """§5: retry only transient classes, and only within that class's own
    budget. Deterministic failures return False on the first attempt."""
    policy = lookup(classification)
    return policy.retry and attempts < policy.max_attempts


# --- verification (§17) ------------------------------------------------


def verify_recovery(conn: sqlite3.Connection, job_id: int, checks) -> tuple[bool, list]:
    """Confirm a job's postconditions actually hold.

    Returns (ok, failed_checks). A recovery that cannot be verified is not
    a recovery -- the caller should escalate rather than declare success.
    """
    failed = []
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    for check in checks:
        if check == "job_not_running":
            if row is None or row["status"] == "running":
                failed.append(check)
        elif check == "lease_released":
            if row is None or row["lease_id"] is not None:
                failed.append(check)
        elif check == "job_row_exists":
            if row is None:
                failed.append(check)
        else:
            failed.append(f"unknown_check:{check}")
    return (not failed, failed)


# --- failure memory (§18) ---------------------------------------------


def record_failure_memory(
    conn: sqlite3.Connection,
    job_id: int | None,
    classification: str,
    symptom: str,
    target_type: str | None = None,
    target_id: int | None = None,
    successful_remediation: str | None = None,
    failed_remediations: str | None = None,
) -> int:
    """Write what went wrong, what fixed it, and what to do differently.

    Committed immediately: a failure memory that is lost when the daemon
    dies is worth nothing, and it is never part of a larger transaction
    whose rollback should discard it.
    """
    policy = lookup(classification)
    cur = conn.execute(
        "INSERT INTO failure_memories (job_id, classification, symptom, target_type, target_id, "
        "successful_remediation, failed_remediations, preventive_rule, resolved) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job_id,
            classification,
            (symptom or "")[:2000],
            target_type,
            target_id,
            successful_remediation,
            failed_remediations,
            policy.preventive_rule or None,
            1 if successful_remediation else 0,
        ),
    )
    conn.commit()
    return cur.lastrowid


def recurring_failures(conn: sqlite3.Connection, classification: str, limit: int = 5):
    """Prior unresolved failures of the same class, newest first -- so a
    caller can avoid re-attempting a remediation already known to fail."""
    return conn.execute(
        "SELECT * FROM failure_memories WHERE classification = ? AND resolved = 0 "
        "ORDER BY created_at DESC LIMIT ?",
        (classification, limit),
    ).fetchall()
