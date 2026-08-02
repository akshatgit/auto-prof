"""Common interface every AI backend (Codex, Ollama Cloud, ...) implements.

Callers (job execution, create_prof, etc.) depend only on this module --
never on a concrete backend -- so backends are swappable per job kind via
the registry (autoprof/backends/registry.py) without touching callers.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class BackendResult:
    """Normalized outcome of one backend call.

    `rate_limited` and `error` are deliberately independent: a rate limit
    is not a failure (see docs/DESIGN.md §5.1/§5.3) and must never set
    `error`/`is_error`, so job retry-vs-backoff logic downstream can branch
    on `is_error` without also having to check `rate_limited` separately.
    """

    text: str
    model_version: str | None = None
    raw: dict | None = None
    rate_limited: bool = False
    retry_after_seconds: float | None = None
    error: str | None = None

    @property
    def is_error(self) -> bool:
        return self.error is not None


class Backend(ABC):
    """Abstract base every concrete backend must implement."""

    name: str = "unnamed"

    @abstractmethod
    def run(self, prompt: str, **opts) -> BackendResult:
        """Execute `prompt` against this backend and return a BackendResult.

        Must never raise for expected failure modes (rate limits, API
        errors) -- those are reported via the BackendResult fields so
        callers have one uniform way to branch on outcome regardless of
        which backend ran. Only truly unexpected conditions (e.g. a bug)
        should raise.
        """
        raise NotImplementedError
