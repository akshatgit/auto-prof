"""Idempotent artifact writes -- docs/DESIGN.md §5.2.

Every file a job produces goes through here: write to a temp path in the
same directory, then atomically rename into the final, deterministic
location. A retried job overwrites the same path with new output rather
than accumulating duplicates; a failed write never touches the existing
file.
"""

import os
import tempfile
from pathlib import Path


def write_artifact(path: Path, content: str, _writer=None) -> None:
    """`_writer(file_obj)` is an injection point for tests to simulate a
    write failure; production callers never pass it."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as f:
            if _writer is not None:
                _writer(f)
            else:
                f.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def checkpoint_artifact(path: Path, keep: int = 10) -> Path | None:
    """Snapshot an artifact before it is overwritten (§7).

    Agent memory is rewritten wholesale on every pass, so a single bad
    write destroys everything the agent had established -- which happened
    live: an interrupted run produced empty output, that empty output was
    written over a student's memory.md, and the accumulated research was
    unrecoverable because no prior version existed anywhere.

    Checkpoints live beside the file in a `.checkpoints/` directory, named
    by write order. Returns the checkpoint path, or None if there was
    nothing to snapshot yet.
    """
    path = Path(path)
    if not path.exists():
        return None

    checkpoint_dir = path.parent / ".checkpoints"
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    existing = sorted(checkpoint_dir.glob(f"{path.name}.*"))
    target = checkpoint_dir / f"{path.name}.{len(existing):04d}"
    # copy, not move: the live file must stay readable throughout, since a
    # concurrent reader (the web UI) may be mid-read.
    target.write_bytes(path.read_bytes())

    # Bounded history -- these accumulate on every pass of a long-running
    # task, and the oldest are the least useful.
    for stale in existing[: max(0, len(existing) + 1 - keep)]:
        stale.unlink(missing_ok=True)
    return target


def restore_artifact(path: Path, index: int = -1) -> bool:
    """Restore an artifact from its most recent checkpoint (or `index`).

    Returns False when no checkpoint exists, so a caller can distinguish
    "restored" from "there was nothing to restore".
    """
    path = Path(path)
    checkpoint_dir = path.parent / ".checkpoints"
    if not checkpoint_dir.is_dir():
        return False
    checkpoints = sorted(checkpoint_dir.glob(f"{path.name}.*"))
    if not checkpoints:
        return False
    write_artifact(path, checkpoints[index].read_text(errors="replace"))
    return True
