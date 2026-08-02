"""Codex CLI backend -- shells out to `codex exec`.

Used both for generation and for independent review (docs/DESIGN.md §4);
each call is a fresh subprocess with no shared state, which is what makes
review isolation meaningful.
"""

import os
import re
import subprocess
import tempfile
from pathlib import Path

from .base import Backend, BackendResult

# Generous by default: the long jobs here are writing a full paper and
# reviewing one step-by-step, both of which routinely run past the few
# minutes that suffice for a decomposition. Still well inside the 1800s
# job lease (docs/DESIGN.md §5.2), so a timeout surfaces as a job-level
# error rather than as a silently reclaimed lease.
DEFAULT_TIMEOUT_SECONDS = 900

# Matches CLI phrasing like "try again in 45s" / "retry after 3m" / "in 2h".
_RETRY_AFTER_RE = re.compile(r"(?:try again|retry)[^0-9]*?(\d+)\s*(s|sec|m|min|h|hour)", re.IGNORECASE)
_RATE_LIMIT_MARKERS = ("rate limit", "rate-limited", "usage limit", "429")

_UNIT_SECONDS = {"s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hour": 3600}


def _parse_retry_after(text: str) -> float | None:
    match = _RETRY_AFTER_RE.search(text)
    if not match:
        return None
    amount, unit = match.groups()
    return float(amount) * _UNIT_SECONDS[unit.lower()]


def _looks_rate_limited(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _RATE_LIMIT_MARKERS)


class CodexBackend(Backend):
    name = "codex"

    def __init__(self, model=None, sandbox="read-only", timeout=None, runner=subprocess.run):
        self.model = model
        self.sandbox = sandbox
        if timeout is None:
            timeout = float(os.environ.get("AUTOPROF_CODEX_TIMEOUT", DEFAULT_TIMEOUT_SECONDS))
        self.timeout = timeout
        self.runner = runner

    def run(self, prompt: str, **opts) -> BackendResult:
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_path = Path(tmp_dir) / "codex_output.txt"
            cmd = [
                "codex", "exec",
                "--skip-git-repo-check",
                "--sandbox", opts.get("sandbox", self.sandbox),
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
            except subprocess.TimeoutExpired:
                return BackendResult(text="", error=f"codex exec timed out after {self.timeout}s")
            except FileNotFoundError:
                return BackendResult(text="", error="`codex` CLI not found on PATH")

            combined_output = f"{proc.stdout or ''}\n{proc.stderr or ''}"

            if proc.returncode != 0:
                if _looks_rate_limited(combined_output):
                    return BackendResult(
                        text="",
                        rate_limited=True,
                        retry_after_seconds=_parse_retry_after(combined_output),
                    )
                return BackendResult(text="", error=(proc.stderr or proc.stdout or "").strip())

            text = out_path.read_text() if out_path.exists() else ""
            return BackendResult(text=text, model_version=model or "codex-default")
