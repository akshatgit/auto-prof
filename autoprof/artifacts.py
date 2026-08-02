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
