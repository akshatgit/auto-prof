"""Lab policy settings read from autoprof.toml.

Separate from autoprof/backends/registry.py's config handling: that
answers "which model runs this job kind", this answers "when has the lab
done enough". Both read the same file; keeping them apart means lab policy
doesn't drag the backend classes into its import graph.
"""

import os
import tomllib
from pathlib import Path

from . import db

# How many ACCEPTED papers a lab is working toward. This is the stopping
# condition for the revise-and-resubmit loop: a rejected paper keeps being
# revised until the lab has this many accepted papers, rather than being
# abandoned after a fixed number of rounds. Rounds measure effort spent;
# accepted papers measure what the lab actually has to show for it, and
# the latter is what a lab exists to produce.
DEFAULT_MAX_ACCEPTED_PAPERS = 4

# How many tasks a professor may open in one decomposition. Each costs a
# student and a full chain of model calls, so the default keeps a lab on a
# workable front; a lab that genuinely spans several independent problems
# raises it.
DEFAULT_MAX_TASKS_PER_DECOMPOSITION = 4

# Ceiling on student<->professor supervision meetings for one task. This is
# a termination guarantee, not a target: the loop is meant to run until the
# professor agrees the work is ready, however many passes that takes, and
# hitting this cap forces a write-up of what exists rather than discarding
# it. Set generously -- real supervision is long-horizon.
DEFAULT_MAX_SUPERVISION_ROUNDS = 12

# Ceiling on collaboration rounds. Same reasoning as supervision: a
# backstop so a collaboration that never converges terminates, not a
# target. Lower than supervision because each round costs one model call
# per member, so the cost grows with the author count.
DEFAULT_MAX_COLLABORATION_ROUNDS = 6

_CONFIG_PATH = db.REPO_ROOT / "autoprof.toml"


def _load(config_path: Path | None = None) -> dict:
    path = Path(config_path) if config_path is not None else _CONFIG_PATH
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


def max_accepted_papers(config_path: Path | None = None, env: dict | None = None) -> int:
    """Target number of accepted papers per lab.

    Precedence, most specific first: AUTOPROF_MAX_ACCEPTED_PAPERS env var,
    `[lab] max_accepted_papers` in autoprof.toml, then the default. Mirrors
    the backend registry's precedence order so the two config surfaces
    behave the same way.

    A value below 1 is meaningless (a lab that wants zero papers has
    nothing to do) and is clamped up to 1 rather than silently disabling
    the loop.
    """
    env = env if env is not None else os.environ
    raw = env.get("AUTOPROF_MAX_ACCEPTED_PAPERS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass

    configured = _load(config_path).get("lab", {}).get("max_accepted_papers")
    if configured is not None:
        try:
            return max(1, int(configured))
        except (TypeError, ValueError):
            pass

    return DEFAULT_MAX_ACCEPTED_PAPERS


def max_supervision_rounds(config_path: Path | None = None, env: dict | None = None) -> int:
    """Ceiling on supervision meetings per task. See
    DEFAULT_MAX_SUPERVISION_ROUNDS for why this is a backstop rather than a
    target."""
    env = env if env is not None else os.environ
    raw = env.get("AUTOPROF_MAX_SUPERVISION_ROUNDS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass

    configured = _load(config_path).get("lab", {}).get("max_supervision_rounds")
    if configured is not None:
        try:
            return max(1, int(configured))
        except (TypeError, ValueError):
            pass

    return DEFAULT_MAX_SUPERVISION_ROUNDS


def max_collaboration_rounds(config_path: Path | None = None, env: dict | None = None) -> int:
    """Ceiling on collaboration rounds. A backstop; hitting it writes the
    joint paper rather than discarding the work."""
    env = env if env is not None else os.environ
    raw = env.get("AUTOPROF_MAX_COLLABORATION_ROUNDS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    configured = _load(config_path).get("lab", {}).get("max_collaboration_rounds")
    if configured is not None:
        try:
            return max(1, int(configured))
        except (TypeError, ValueError):
            pass
    return DEFAULT_MAX_COLLABORATION_ROUNDS


def max_tasks_per_decomposition(config_path: Path | None = None, env: dict | None = None) -> int:
    """Tasks opened per decomposition. See
    DEFAULT_MAX_TASKS_PER_DECOMPOSITION for why this is capped at all."""
    env = env if env is not None else os.environ
    raw = env.get("AUTOPROF_MAX_TASKS")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    configured = _load(config_path).get("lab", {}).get("max_tasks_per_decomposition")
    if configured is not None:
        try:
            return max(1, int(configured))
        except (TypeError, ValueError):
            pass
    return DEFAULT_MAX_TASKS_PER_DECOMPOSITION
