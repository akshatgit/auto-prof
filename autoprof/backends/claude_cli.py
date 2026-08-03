"""Claude Code CLI backend -- shells out to `claude -p`.

Exists for one reason: **reviewer independence requires model diversity.**

Three Codex reviewers are three processes, not three opinions. They share
training data, refusal boundaries and blind spots, so a claim that reads
as sound to one reads as sound to all three, and a 3-of-3 accept measures
agreement within one model family rather than correctness. The collaborating
`refute-or-promote` work has the clean counterexample: ten dedicated
reviewers unanimously endorsed a Bleichenbacher padding oracle that did not
exist, and only an empirical test killed it. Convergence among like
reviewers is not calibration.

Mixing a Claude reviewer into the panel does not make any single review
better -- it makes the panel's errors less correlated, which is the only
thing that makes a 2-of-3 vote mean anything.

Sandboxing note: reviewers run with `--permission-mode plan`, which is
read-only by construction. A reviewer that could edit files could 'fix'
the paper it was judging.
"""

import json
import os
import re
import subprocess

from .base import Backend, BackendResult

# Same reasoning as the Codex backend: our own wall-clock ceiling is not
# the model's, and killing a long review discards work already paid for.
# Set AUTOPROF_CLAUDE_TIMEOUT to reinstate one.
DEFAULT_TIMEOUT_SECONDS = None

_RETRY_AFTER_RE = re.compile(
    r"(?:try again|retry|resets?)[^0-9]*?(\d+)\s*(s|sec|m|min|h|hour)", re.IGNORECASE
)
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate-limited",
    "usage limit",
    "429",
    "overloaded",
    "please wait",
)
# Context exhaustion is reported as rate-limited, not as an error: it is
# not a defect in the work, must not burn a retry attempt, and the session
# is resumable so the next attempt continues rather than re-deriving.
_TOKEN_EXHAUSTION_MARKERS = (
    "context length",
    "context window",
    "maximum context",
    "prompt is too long",
    "token limit",
    "insufficient_quota",
)

_UNIT_SECONDS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hour": 3600}
_UNSET = object()


def _parse_retry_after(text: str) -> float | None:
    match = _RETRY_AFTER_RE.search(text)
    if not match:
        return None
    amount, unit = match.groups()
    return float(amount) * _UNIT_SECONDS[unit.lower()]


def _looks_rate_limited(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS) or any(
        marker in lowered for marker in _TOKEN_EXHAUSTION_MARKERS
    )


def parse_result(stdout: str) -> tuple[str, str | None, bool]:
    """Pull (text, session_id, is_error) out of `--output-format json`.

    Returns the session id even when the run errored -- that is precisely
    when the next attempt needs it. A stdout we cannot parse is reported
    as an error rather than as empty success, because callers write
    results straight over an agent's memory.
    """
    stdout = (stdout or "").strip()
    if not stdout:
        return "", None, True

    payload = None
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        # Tolerate a stream-json tail or leading noise: take the last
        # line that parses as an object.
        for line in reversed(stdout.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if not isinstance(payload, dict):
        return "", None, True

    session_id = payload.get("session_id")
    is_error = bool(payload.get("is_error"))
    text = payload.get("result") or ""
    if not isinstance(text, str):
        text = json.dumps(text)
    return text, session_id, is_error


class ClaudeBackend(Backend):
    """`claude -p` as a review backend.

    Defaults to `plan` permission mode: a reviewer reads and judges, and
    must not be able to modify the artifact under review.
    """

    name = "claude"

    def __init__(
        self,
        model=None,
        permission_mode="plan",
        timeout=_UNSET,
        runner=subprocess.run,
    ):
        self.model = model or os.environ.get("AUTOPROF_CLAUDE_MODEL") or None
        self.permission_mode = permission_mode
        if timeout is _UNSET:
            configured = os.environ.get("AUTOPROF_CLAUDE_TIMEOUT")
            timeout = float(configured) if configured else DEFAULT_TIMEOUT_SECONDS
        self.timeout = timeout
        self.runner = runner

    def run(self, prompt: str, **opts) -> BackendResult:
        resume_session_id = opts.get("resume_session_id")

        cmd = ["claude", "-p", "--output-format", "json"]
        if resume_session_id:
            cmd += ["--resume", resume_session_id]
        cmd += ["--permission-mode", opts.get("permission_mode", self.permission_mode)]
        model = opts.get("model", self.model)
        if model:
            cmd += ["--model", model]
        cmd.append(prompt)

        try:
            # stdin=DEVNULL for the same reason as Codex: `claude -p` reads
            # the prompt from stdin when one is attached, and a daemon
            # inherits a pipe nobody writes to -- the call then blocks
            # until the timeout with no useful error.
            proc = self.runner(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as e:
            partial = getattr(e, "output", None) or ""
            if isinstance(partial, bytes):
                partial = partial.decode(errors="replace")
            _, session_id, _ = parse_result(partial)
            return BackendResult(
                text="",
                error=f"claude -p timed out after {self.timeout}s",
                session_id=session_id or resume_session_id,
            )
        except FileNotFoundError:
            return BackendResult(text="", error="`claude` CLI not found on PATH")

        text, session_id, payload_error = parse_result(proc.stdout or "")
        session_id = session_id or resume_session_id
        combined = f"{proc.stdout or ''}\n{proc.stderr or ''}"

        if proc.returncode != 0 or payload_error:
            if _looks_rate_limited(combined) or _looks_rate_limited(text):
                return BackendResult(
                    text="",
                    rate_limited=True,
                    retry_after_seconds=_parse_retry_after(combined) or _parse_retry_after(text),
                    session_id=session_id,
                )
            return BackendResult(
                text="",
                error=(text or proc.stderr or proc.stdout or "claude -p failed").strip()[:2000],
                session_id=session_id,
            )

        # A clean exit with no text is a failure, not an empty success --
        # callers overwrite memory files with whatever comes back.
        if not text.strip():
            return BackendResult(
                text="",
                error="claude -p produced no output (exited cleanly but wrote nothing)",
                session_id=session_id,
            )

        return BackendResult(
            text=text,
            model_version=model or "claude-default",
            session_id=session_id,
        )
