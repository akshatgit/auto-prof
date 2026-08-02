"""Codex CLI backend -- shells out to `codex exec`.

Used both for generation and for independent review (docs/DESIGN.md §4);
each call is a fresh subprocess with no shared state, which is what makes
review isolation meaningful.
"""

import re
import subprocess
import tempfile
from pathlib import Path

from .base import Backend, BackendResult

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

    def __init__(self, model=None, sandbox="read-only", timeout=280, runner=subprocess.run):
        self.model = model
        self.sandbox = sandbox
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
                proc = self.runner(cmd, capture_output=True, text=True, timeout=self.timeout)
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
