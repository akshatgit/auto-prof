"""Codex CLI backend -- shells out to `codex exec`.

Used both for generation and for independent review (docs/DESIGN.md §4);
each call is a fresh subprocess with no shared state, which is what makes
review isolation meaningful.
"""

import json
import os
import re
import subprocess

from .process import run_process
import tempfile
from datetime import datetime, timezone
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

# Seconds of COMPLETE SILENCE before a call is abandoned. This replaces the
# wall-clock kill: a job still emitting tokens is working however long it
# takes, and killing it at a fixed 40 minutes destroyed healthy research
# rounds. A job that has produced nothing for this long is the hung one.
DEFAULT_IDLE_TIMEOUT_SECONDS = 900

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

# Debug output is useful for OpenAI support, but it can contain response
# headers (including cookies).  Raw RUST_LOG output must therefore never be
# copied into the job database or a support artifact.  The diagnostic path
# below stores only a small allowlist of correlation fields.
_REQUEST_ID_RE = re.compile(r"\breq_[A-Za-z0-9_-]{8,}\b")
_SENSITIVE_FIELD_RE = re.compile(
    r"(?i)([\"']?(?:authorization|cookie|set-cookie|x-api-key|api-key|"
    r"access_token|refresh_token)[\"']?\s*[:=]\s*)"
    r"(?:[\"'][^\"']*[\"']|[^,}\s]+)"
)
_MAX_ERROR_CHARS = 20_000


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


def parse_final_message(stdout: str) -> str:
    """Extract the agent's last message from `codex exec --json` output.

    The stream carries one JSON object per line; the answer arrives as
    `{"type":"item.completed","item":{"type":"agent_message","text":...}}`.
    Takes the LAST such item, since a run may emit several.
    """
    latest = ""
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") or {}
        if item.get("type") == "agent_message" and item.get("text"):
            latest = item["text"]
    return latest


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


def _redact_sensitive(text: str) -> str:
    """Remove common credential/header values from subprocess diagnostics."""
    return _SENSITIVE_FIELD_RE.sub(r"\1[REDACTED]", text or "")


def _json_error_messages(stdout: str) -> list[str]:
    """Extract user-facing errors from the CLI JSONL stream."""
    messages: list[str] = []
    for line in (stdout or "").splitlines():
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = [event.get("message")]
        error = event.get("error")
        if isinstance(error, dict):
            candidates.append(error.get("message"))
        for message in candidates:
            if isinstance(message, str) and message.strip() and message not in messages:
                messages.append(message.strip())
    return messages


def _safe_error_text(stdout: str, stderr: str) -> str:
    """Return a concise error without persisting raw debug headers."""
    messages = _json_error_messages(stdout)
    if messages:
        return "\n".join(_redact_sensitive(message) for message in messages)[-_MAX_ERROR_CHARS:]
    fallback = _redact_sensitive((stderr or stdout or "").strip())
    return fallback[-_MAX_ERROR_CHARS:]


def _looks_like_missing_session(output: str) -> bool:
    """Did Codex refuse because the session we asked to resume is gone?

    Rollouts live on disk under CODEX_HOME and are not permanent: they get
    cleaned up, and a Codex upgrade can invalidate them. When that happens
    every later job for that agent fails identically, because the dead
    thread id is stored in the database and handed back on each attempt --
    a permanent stall from a recoverable condition. Observed live: job 1310
    failed with "no rollout found for thread id".
    """
    lowered = output.lower()
    return (
        "no rollout found for thread" in lowered
        or ("thread/resume" in lowered and "failed" in lowered)
        or "session not found" in lowered
    )


def _session_transcript(codex_home: Path, session_id: str | None) -> Path | None:
    if not session_id:
        return None
    matches = list((codex_home / "sessions").glob(f"**/rollout-*-{session_id}.jsonl"))
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def _diagnostic_events(stdout: str, transcript: Path | None) -> dict:
    """Collect only correlation-safe fields from JSONL, never raw content."""
    sources = [("cli", stdout.splitlines())]
    if transcript is not None:
        try:
            sources.append(("session", transcript.read_text().splitlines()))
        except OSError:
            pass

    turn_ids: set[str] = set()
    response_item_ids: set[str] = set()
    classifier_codes: set[str] = set()
    errors: set[str] = set()
    timestamps: set[str] = set()
    for _source, lines in sources:
        for line in lines:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            timestamp = record.get("timestamp")
            if isinstance(timestamp, str):
                timestamps.add(timestamp)
            payload = record.get("payload") if isinstance(record.get("payload"), dict) else record
            item_id = payload.get("id")
            if isinstance(item_id, str) and item_id.startswith("rs_"):
                response_item_ids.add(item_id)
            turn_id = payload.get("turn_id")
            if isinstance(turn_id, str):
                turn_ids.add(turn_id)
            error = payload.get("error")
            if isinstance(error, dict):
                code = error.get("codex_error_info")
                message = error.get("message")
                if isinstance(code, str):
                    classifier_codes.add(code)
                if isinstance(message, str):
                    errors.add(_redact_sensitive(message))
            message = payload.get("message")
            if isinstance(message, str) and (
                "cybersecurity risk" in message.lower() or "cyber_policy" in message.lower()
            ):
                errors.add(_redact_sensitive(message))
    return {
        "turn_ids": sorted(turn_ids),
        "response_item_ids": sorted(response_item_ids),
        "classifier_codes": sorted(classifier_codes),
        "errors": sorted(errors),
        "event_timestamps": sorted(timestamps),
    }


def _write_diagnostic_report(
    directory: Path,
    *,
    stdout: str,
    stderr: str,
    returncode: int,
    session_id: str | None,
    model: str | None,
    support_case: str | None,
    codex_home: Path,
) -> Path | None:
    """Persist an allowlisted support report; failure must not mask the job result."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        transcript = _session_transcript(codex_home, session_id)
        events = _diagnostic_events(stdout, transcript)
        request_ids = sorted(set(_REQUEST_ID_RE.findall(f"{stdout}\n{stderr}")))
        report = {
            "schema": "autoprof.codex-support-diagnostic.v1",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "support_case": support_case,
            "session_id": session_id,
            "model": model or "codex-default",
            "returncode": returncode,
            # Debug streams can include unrelated analytics requests.  Do
            # not mislabel a captured req_* value as the model request.
            "request_ids_unattributed": request_ids,
            "request_id_caveat": (
                "Captured req_* values may identify telemetry or another HTTP request; "
                "OpenAI Support must correlate them before attribution."
            ),
            **events,
        }
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        suffix = session_id or "unknown-session"
        path = directory / f"codex-failure-{stamp}-{suffix}.json"
        path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        return path
    except OSError:
        return None


class CodexBackend(Backend):
    name = "codex"

    def __init__(self, model=None, sandbox="read-only", timeout=_UNSET,
                 idle_timeout=_UNSET, runner=run_process):
        self.model = model
        self.sandbox = sandbox
        if timeout is _UNSET:
            configured = os.environ.get("AUTOPROF_CODEX_TIMEOUT")
            timeout = float(configured) if configured else DEFAULT_TIMEOUT_SECONDS
        if idle_timeout is _UNSET:
            configured = os.environ.get("AUTOPROF_CODEX_IDLE_TIMEOUT")
            idle_timeout = float(configured) if configured else DEFAULT_IDLE_TIMEOUT_SECONDS
        self.idle_timeout = idle_timeout
        # None means no wall-clock limit -- subprocess.run treats
        # timeout=None as "wait indefinitely", which is what we want.
        self.timeout = timeout
        self.runner = runner

    def run(self, prompt: str, **opts) -> BackendResult:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir) / "codex_output.txt"
            resume_session_id = opts.get("resume_session_id")

            # `codex exec resume` accepts a NARROWER flag set than `codex
            # exec` -- notably neither --sandbox nor -o. Assuming parity
            # made every resume fail with "unexpected argument", which
            # mocked tests could not catch because they validated the
            # command we intended rather than one Codex accepts.
            resuming = bool(resume_session_id)
            sandbox = opts.get("sandbox", self.sandbox)
            cwd = opts.get("cwd")
            cmd = ["codex", "exec"]
            if resuming:
                cmd += ["resume", resume_session_id]
            cmd += ["--skip-git-repo-check", "--json"]
            if not resuming:
                cmd += ["--sandbox", sandbox, "-o", str(out_path)]
                if cwd:
                    cmd += ["-C", str(cwd)]
            else:
                # `codex exec resume` accepts neither --sandbox nor -C, so a
                # resumed session silently fell back to codex's own default:
                # read-only, in the DAEMON's directory. Every attempt after
                # the first therefore lost Docker and lost the workspace,
                # and the student truthfully reported both as unavailable.
                # -c restores the sandbox; the child's own cwd restores the
                # directory, since -C is unavailable here.
                cmd += ["-c", f'sandbox_mode="{sandbox}"']
            model = opts.get("model", self.model)
            if model:
                cmd += ["--model", model]
            # Never put the prompt in argv. Linux limits each individual
            # argument to MAX_ARG_STRLEN (normally 128 KiB), which stranded
            # paper 55 once the document plus rubric reached 143 KiB even
            # though ARG_MAX was much larger. `codex exec -` explicitly
            # reads the prompt from stdin and has no per-argument ceiling.
            cmd.append("-")

            diagnostics_value = os.environ.get("AUTOPROF_CODEX_DIAGNOSTICS_DIR")
            diagnostics_dir = Path(diagnostics_value) if diagnostics_value else None
            child_env = None
            if diagnostics_dir is not None:
                child_env = os.environ.copy()
                child_env["RUST_LOG"] = os.environ.get(
                    "AUTOPROF_CODEX_RUST_LOG",
                    "codex_api=trace,codex_http_client=debug,codex_core=info",
                )

            try:
                # Supplying `input` both closes stdin deterministically and
                # carries arbitrarily large prompts without shell/argv
                # limits. Do not replace this with an inherited stdin: a
                # daemon pipe nobody writes to blocks forever.
                proc = self.runner(
                    cmd,
                    capture_output=True,
                    text=True, errors="replace",
                    timeout=self.timeout,
                    idle_timeout=self.idle_timeout,
                    on_progress=opts.get("on_progress"),
                    input=prompt,
                    **({"cwd": str(cwd)} if cwd else {}),
                    **({"env": child_env} if child_env is not None else {}),
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

            diagnostic_path = None
            if proc.returncode != 0 and diagnostics_dir is not None:
                codex_home = Path(
                    (child_env or os.environ).get("CODEX_HOME", str(Path.home() / ".codex"))
                )
                diagnostic_path = _write_diagnostic_report(
                    diagnostics_dir,
                    stdout=proc.stdout or "",
                    stderr=proc.stderr or "",
                    returncode=proc.returncode,
                    session_id=session_id,
                    model=model,
                    support_case=os.environ.get("AUTOPROF_CODEX_SUPPORT_CASE"),
                    codex_home=codex_home,
                )

            if proc.returncode != 0:
                # A dead rollout is not a failed run, it is a lost thread.
                # Start a fresh session rather than failing this job and
                # every job after it. The prompt already carries the
                # agent's memory, so the cost is lost conversational
                # context, not lost research.
                if (
                    resuming
                    and _looks_like_missing_session(combined_output)
                    and not opts.get("_session_restarted")
                ):
                    fresh = dict(opts)
                    fresh.pop("resume_session_id", None)
                    fresh["_session_restarted"] = True
                    return self.run(prompt, **fresh)
                if _looks_rate_limited(combined_output) or _looks_token_exhausted(combined_output):
                    return BackendResult(
                        text="",
                        rate_limited=True,
                        retry_after_seconds=_parse_retry_after(combined_output),
                        session_id=session_id,
                        raw=(
                            {"diagnostic_path": str(diagnostic_path)}
                            if diagnostic_path is not None else None
                        ),
                    )
                return BackendResult(
                    text="",
                    error=_safe_error_text(proc.stdout or "", proc.stderr or ""),
                    session_id=session_id,
                    raw=(
                        {"diagnostic_path": str(diagnostic_path)}
                        if diagnostic_path is not None else None
                    ),
                )

            # On a resume there is no -o file, so the answer comes from the
            # event stream. Fall back to it on a fresh run too: an empty
            # -o file with a well-formed stream is recoverable.
            text = out_path.read_text() if out_path.exists() else ""
            if not text.strip():
                text = parse_final_message(proc.stdout or "")

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
