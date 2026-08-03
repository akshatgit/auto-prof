"""Config-driven backend selection: which Backend handles which jobs.kind.

Precedence, most specific wins: per-kind env var > per-kind config
override > category env var > category config default > hardcoded
fallback. The hardcoded fallback matches the /goal directive -- Codex and
Ollama Cloud as the two backends, review isolation stays on Codex
(docs/DESIGN.md §4 depends on independent reviewer processes), generation
defaults to Ollama Cloud.
"""

import os
import tomllib
from pathlib import Path

from .base import Backend
from .claude_cli import ClaudeBackend
from .codex import CodexBackend
from .ollama_cloud import OllamaCloudBackend

REVIEW_KINDS = {"paper_review", "defense_review", "lab_review"}
GENERATION_KINDS = {
    "professor_decompose",
    "student_work",
    "student_write_paper",
    "student_revise_paper",
    "professor_supervision",
    "collaboration_round",
    "collaboration_synthesis",
    "collaboration_write_paper",
    "reference_verify",
    "professor_callback",
    "lab_revise",
    "collaboration_scan",
    "student_write_defense",
    "propose_lab",
    "memory_compact",
}

DEFAULT_BACKEND_FOR_CATEGORY = {"generation": "ollama_cloud", "review": "codex"}

BACKEND_CLASSES: dict[str, type[Backend]] = {
    "codex": CodexBackend,
    "claude": ClaudeBackend,
    "ollama_cloud": OllamaCloudBackend,
}

# The review PANEL: which backend runs reviewer #1, #2, #3 ...
#
# Reviewer independence is the load-bearing assumption behind every review
# gate in this system -- 2-of-3 on a paper, 4-of-5 on a defense -- and
# three processes of the SAME model are not three independent opinions.
# They share training data and blind spots, so a flaw invisible to one is
# invisible to all three and unanimity measures family agreement, not
# correctness. Mixing families does not make any single review better; it
# decorrelates the panel's errors, which is the only thing that makes the
# vote informative.
#
# The panel cycles if there are more reviewers than entries, so a 5-member
# defense panel off a 2-entry panel is codex, claude, codex, claude, codex
# -- never all one family.
DEFAULT_REVIEW_PANEL = ("codex", "claude", "codex")


def review_panel(config: dict, env: dict) -> list[str]:
    """Ordered backend names for reviewers 1..N. Env wins over config."""
    raw = env.get("AUTOPROF_REVIEW_PANEL") or ""
    if raw.strip():
        names = [part.strip() for part in raw.split(",") if part.strip()]
        if names:
            return names
    configured = config.get("backends", {}).get("review_panel")
    if configured:
        return [str(name) for name in configured]
    return list(DEFAULT_REVIEW_PANEL)


def classify_kind(kind: str) -> str:
    if kind in REVIEW_KINDS:
        return "review"
    if kind in GENERATION_KINDS:
        return "generation"
    raise ValueError(f"unknown job kind: {kind!r}")


def resolve_backend_name(
    kind: str, config: dict, env: dict, reviewer_index: int | None = None
) -> str:
    per_kind_env_key = f"AUTOPROF_BACKEND_{kind.upper()}"
    if env.get(per_kind_env_key):
        return env[per_kind_env_key]

    overrides = config.get("backends", {}).get("overrides", {})
    if kind in overrides:
        return overrides[kind]

    category = classify_kind(kind)

    # Panel assignment sits below the explicit per-kind overrides (so
    # pinning a kind to one backend still works) but above the category
    # default, because a mixed panel IS the review default.
    if category == "review" and reviewer_index:
        panel = review_panel(config, env)
        if panel:
            return panel[(reviewer_index - 1) % len(panel)]

    category_env_key = f"AUTOPROF_{category.upper()}_BACKEND"
    if env.get(category_env_key):
        return env[category_env_key]

    defaults = config.get("backends", {}).get("default", {})
    if category in defaults:
        return defaults[category]

    return DEFAULT_BACKEND_FOR_CATEGORY[category]


def load_config(path: Path | None) -> dict:
    if path is None:
        return {}
    path = Path(path)
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


class Registry:
    """Resolves a jobs.kind to a live Backend instance, caching one
    instance per backend name so e.g. all 3 paper reviewers still get
    fresh independent `.run()` calls but don't pay backend-construction
    cost repeatedly."""

    def __init__(self, config: dict | None = None, env: dict | None = None, backend_classes=None):
        self.config = config if config is not None else {}
        self.env = env if env is not None else dict(os.environ)
        self.backend_classes = backend_classes if backend_classes is not None else BACKEND_CLASSES
        self._instances: dict[str, Backend] = {}

    def get_backend(self, kind: str, reviewer_index: int | None = None) -> Backend:
        name = resolve_backend_name(kind, self.config, self.env, reviewer_index)
        if name not in self._instances:
            cls = self.backend_classes.get(name)
            if cls is None:
                raise ValueError(
                    f"unknown backend {name!r} configured for job kind {kind!r} "
                    f"(known backends: {sorted(self.backend_classes)})"
                )
            self._instances[name] = cls()
        return self._instances[name]


def default_registry(config_path: Path | None = None) -> Registry:
    config = load_config(config_path)
    return Registry(config=config)
