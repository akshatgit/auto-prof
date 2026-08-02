"""Codex CLI backend -- shells out to `codex exec`.

Used both for generation and for independent review (docs/DESIGN.md §4);
each call is a fresh subprocess with no shared state, which is what makes
review isolation meaningful.
"""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .base import Backend, BackendResult

# No wall-clock limit by default.
#
# The previous 900s ceiling was OUR kill switch, not Codex's -- and it was
# actively destructive: a student_work job on a hard derivation was killed
# three times at exactly 900s while Codex was still making progress, each
# kill discarding the entire partial derivation and burning a retry
# attempt. The model's real constraints (usage limits, context exhaustion)
# are reported by Codex itself and handled below; imposing a second,
# arbitrary deadline on top of them only threw away work we had paid for.
#
# Set AUTOPROF_CODEX_TIMEOUT to a number of seconds to reinstate one.
DEFAULT_TIMEOUT_SECONDS = None

# Matches CLI phrasing like "try again in 45s" / "retry after 3m" / "in 2h".
_RETRY_AFTER_RE = re.compile(r"(?:try again|retry)[^0-9]*?(\d+)\s*(s|sec|m|min|h|hour)", re.IGNORECASE)
_RATE_LIMIT_MARKERS = ("rate limit", "rate-limited", "usage limit", "429")

# Token/context exhaustion. Treated as rate-limited rather than as an
# error on purpose: like a rate limit it is not a defect in the work, it
# must not burn a retry attempt, and -- crucially -- the session is still
# resumable, so the next attempt continues from where this one stopped
# instead of re-deriving everything.
_TOKEN_EXHAUSTION_MARKERS = (
    "context length",
    "context window",
    "maximum context",
    "token limit",
    "out of tokens",
    "token budget",
    "insufficient_quota",
)

_UNIT_SECONDS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hour": 3600}

# Distinguishes "caller passed no timeout" from "caller explicitly passed
# None", which now means something specific (run with no wall-clock limit).
_UNSET = object()


def _parse_retry_after(text: str) -> float | None:
    match = _RETRY_AFTER_RE.search(text)
    if not match:
        return None
    amount, unit = match.groups()
    return float(amount) * _UNIT_SECONDS[unit.lower()]


def _looks_rate_limited(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


def _looks_token_exhausted(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _TOKEN_EXHAUSTION_MARKERS)


def parse_session_id(stdout: str) -> str | None:
    """Pull the thread id out of `codex exec --json`'s JSONL event stream.

    The id arrives in the first event (`{"type":"thread.started",
    "thread_id":"..."}`), so it is available even when the run later dies
    -- which is exactly the case resumption exists for. Non-JSON lines are
    skipped rather than fatal: the stream is a CLI's stdout, not a
    contract, and losing the id must degrade to "start fresh", never to
    "crash the job".
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        thread_id = event.get("thread_id")
        if thread_id:
            return thread_id
    return None


class CodexBackend(Backend):
    name = "codex"

    def __init__(self, model=None, sandbox="read-only", timeout=_UNSET, runner=subprocess.run):
        self.model = model
        self.sandbox = sandbox
        if timeout is _UNSET:
            configured = os.environ.get("AUTOPROF_CODEX_TIMEOUT")
            timeout = float(configured) if configured else DEFAULT_TIMEOUT_SECONDS
        # None means no wall-clock limit -- subprocess.run treats
        # timeout=None as "wait indefinitely", which is what we want.
        self.timeout = timeout
        self.runner = runner

    def run(self, prompt: str, **opts) -> BackendResult:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir) / "codex_output.txt"
            resume_session_id = opts.get("resume_session_id")

            cmd = ["codex", "exec"]
            if resume_session_id:
                # `codex exec resume <id> <prompt>` continues the existing
                # thread, so a retry after token exhaustion picks up the
                # derivation instead of starting from nothing.
                cmd += ["resume", resume_session_id]
            cmd += [
                "--skip-git-repo-check",
                "--sandbox", opts.get("sandbox", self.sandbox),
                # --json makes stdout a JSONL event stream whose first
                # event carries thread_id; -o still captures the final
                # message, so we get both the id and the answer.
                "--json",
                "-o", str(out_path),
            ]
            model = opts.get("model", self.model)
            if model:
                cmd += ["--model", model]
            cmd.append(prompt)

            try:
                # stdin=DEVNULL is load-bearing, not hygiene: `codex exec`
                # will read additional prompt input from stdin, and
                # subprocess inherits the parent's stdin by default. Run
                # from a daemon (or any context where stdin is an open pipe
                # nobody writes to) it blocks there forever and the call
                # burns the entire timeout before failing -- observed as a
                # 900s hang with "Reading additional input from stdin..."
                # as the only clue.
                proc = self.runner(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    stdin=subprocess.DEVNULL,
                )
            except subprocess.TimeoutExpired as e:
                # Only reachable when a timeout was explicitly configured.
                # Salvage the session id from whatever was printed before
                # the kill so the retry can resume rather than restart.
                partial = getattr(e, "output", None) or ""
                if isinstance(partial, bytes):
                    partial = partial.decode(errors="replace")
                return BackendResult(
                    text="",
                    error=f"codex exec timed out after {self.timeout}s",
                    session_id=parse_session_id(partial) or resume_session_id,
                )
            except FileNotFoundError:
                return BackendResult(text="", error="`codex` CLI not found on PATH")

            combined_output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
            # Prefer the id this run reported; fall back to the one we
            # resumed from, so the chain survives a run that dies before
            # emitting thread.started.
            session_id = parse_session_id(proc.stdout or "") or resume_session_id

            if proc.returncode != 0:
                if _looks_rate_limited(combined_output) or _looks_token_exhausted(combined_output):
                    return BackendResult(
                        text="",
                        rate_limited=True,
                        retry_after_seconds=_parse_retry_after(combined_output),
                        session_id=session_id,
                    )
                return BackendResult(
                    text="",
                    error=(proc.stderr or proc.stdout or "").strip(),
                    session_id=session_id,
                )

            text = out_path.read_text() if out_path.exists() else ""

            # A zero exit with no output is a failure, not an empty
            # success. `codex exec` writes its answer to the -o file at the
            # end, so a run killed or truncated partway can exit cleanly
            # having written nothing. Reporting that as success is
            # destructive: callers write the result straight over an
            # agent's memory.md, so an empty "success" silently erases
            # accumulated research and hands the next job an empty file to
            # write a paper from. Observed exactly that way live.
            if not text.strip():
                return BackendResult(
                    text="",
                    error="codex exec produced no output (exited cleanly but wrote nothing)",
                    session_id=session_id,
                )

            return BackendResult(
                text=text,
                model_version=model or "codex-default",
                session_id=session_id,
            )
