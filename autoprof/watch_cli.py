"""`autoprof watch` -- surface only the things worth interrupting someone for.

The monitoring problem here is not volume, it is signal. A lab generates
hundreds of state changes an hour and almost none of them merit attention;
meanwhile the two events that genuinely need a human -- a lab proposal
awaiting approval, a patch applied to the repository -- look identical to
everything else in the job table.

Worse, the failure that actually happened in this system was SILENCE: the
daemon crashed and nothing changed for an hour, which is indistinguishable
from a quiet period if you only watch for state changes. So this watches
for absence too.

Writes each notable event once to a durable log (so nothing is missed
while nobody is looking) and optionally to the desktop. Runs independently
of any assistant session -- that is the point.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

from . import db

# What is worth an interruption, in descending order of urgency. Anything
# not listed here is deliberately invisible: routine verdicts, individual
# supervision meetings and job dispatches are progress, not news.
NOTABLE_EVENT_TYPES = {
    "lab_proposed": "NEEDS YOU: a graduated student proposes a new lab -- `autoprof lab proposals`",
    "defense_passed": "a student PASSED their defense and will lead a lab",
    "defense_failed": "a student failed their defense",
    "paper_accepted": "a paper was ACCEPTED",
    "lab_review_passed": "a lab passed review and is now active",
    "lab_review_exhausted": (
        "NEEDS YOU: a lab failed review 4 times and has stopped revising -- "
        "push it through, rewrite the root problem, or drop it"
    ),
    "task_resolved": "the professor closed a task as resolved",
    "task_abandoned": "the professor abandoned a task",
    "collaboration_ready": "a collaboration converged and is writing a joint paper",
    "job_failed_terminal": "a job failed permanently",
}


def _notify(message: str, desktop: bool) -> None:
    if not desktop:
        return
    # Best-effort: a missing notify-send must never stop the watcher.
    try:
        subprocess.run(
            ["notify-send", "auto-prof", message[:400]],
            capture_output=True, timeout=10, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        pass


def _load_seen(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text())
        except json.JSONDecodeError:
            pass
    return {"last_event_id": 0, "last_progress_at": time.time()}


def collect(conn, last_event_id: int) -> list[tuple[int, str]]:
    """Notable events newer than `last_event_id`, oldest first."""
    rows = conn.execute(
        "SELECT * FROM events WHERE id > ? ORDER BY id", (last_event_id,)
    ).fetchall()
    out = []
    for row in rows:
        headline = NOTABLE_EVENT_TYPES.get(row["event_type"])
        if headline:
            out.append((row["id"], f"{headline} ({row['target_type']} #{row['target_id']})"))
    return out


def stall_warning(conn, quiet_seconds: float, threshold: float) -> str | None:
    """Warn when nothing has completed for too long WHILE work is queued.

    This is the case that actually bit: a crashed daemon produces no
    events, and no events looks exactly like a quiet period. Pending work
    with no completions is the distinguishing signal.
    """
    if quiet_seconds < threshold:
        return None
    minutes = int(quiet_seconds // 60)
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('pending', 'running')"
    ).fetchone()["n"]
    if pending:
        return (
            f"NOTHING has completed in {minutes} min while {pending} job(s) are "
            "queued -- the daemon may be down. Check `screen -r autoprof-daemon`."
        )

    # The opposite shape, and the one this warning could not see: work in
    # progress with NOTHING queued for it. When the last job for a task
    # fails, nothing enqueues another -- the task stays in_progress, its
    # student stays 'working', and the queue is empty, which is
    # indistinguishable from a healthy idle system. A burst of provider
    # 500s stranded all five of lab #6's tasks exactly this way and no
    # alarm could fire, because every alarm here required pending > 0.
    stranded = conn.execute(
        "SELECT COUNT(*) AS n FROM tasks t WHERE t.status = 'in_progress' "
        "AND NOT EXISTS (SELECT 1 FROM jobs j WHERE j.status IN ('pending','running') "
        "AND j.target_type = 'task' AND j.target_id = t.id)"
    ).fetchone()["n"]
    if stranded:
        return (
            f"{stranded} task(s) are in_progress with NO job queued and nothing has "
            f"completed in {minutes} min -- they are stranded, not idle. Check "
            "`autoprof status` and re-enqueue their work."
        )
    return None


def _cmd_watch(args) -> int:
    state_path = Path(args.state_path)
    log_path = Path(args.log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    state = _load_seen(state_path)
    warned_stalled = False

    print(f"watching {args.db_path}; notable events -> {log_path}", flush=True)
    while True:
        conn = db.connect(args.db_path)
        try:
            db.ensure_initialized(conn)
            events = collect(conn, state["last_event_id"])
            for event_id, message in events:
                stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                line = f"[{stamp}] {message}"
                with log_path.open("a") as handle:
                    handle.write(line + "\n")
                print(line, flush=True)
                _notify(message, args.desktop)
                state["last_event_id"] = event_id

            if events:
                state["last_progress_at"] = time.time()
                warned_stalled = False
            else:
                quiet = time.time() - state.get("last_progress_at", time.time())
                warning = stall_warning(conn, quiet, args.stall_seconds)
                if warning and not warned_stalled:
                    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
                    with log_path.open("a") as handle:
                        handle.write(f"[{stamp}] {warning}\n")
                    print(f"[{stamp}] {warning}", flush=True)
                    _notify(warning, args.desktop)
                    warned_stalled = True  # warn once per stall, not every poll
        finally:
            conn.close()

        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state))
        if args.once:
            return 0
        time.sleep(args.interval)


def add_subparser(subparsers) -> None:
    p = subparsers.add_parser(
        "watch", help="Log and notify on notable lab events; warn if the daemon goes quiet."
    )
    p.add_argument("--db-path", type=Path, default=db.DEFAULT_DB_PATH)
    p.add_argument(
        "--log-path", type=Path, default=Path("notable.log"),
        help="Durable log of notable events, so nothing is missed while nobody is watching.",
    )
    p.add_argument("--state-path", type=Path, default=Path(".autoprof-watch.json"))
    p.add_argument("--interval", type=float, default=60.0)
    p.add_argument(
        "--stall-seconds", type=float, default=1800.0,
        help="Warn if nothing completes for this long while jobs are queued.",
    )
    p.add_argument("--desktop", action="store_true", help="Also send desktop notifications.")
    p.add_argument("--once", action="store_true", help="Poll once and exit (for testing).")
    p.set_defaults(func=_cmd_watch)
