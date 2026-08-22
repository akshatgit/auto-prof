"""Subprocess execution whose timeout actually terminates the work.

`subprocess.run(capture_output=True, timeout=N)` kills only the DIRECT child
and then waits for the stdout/stderr pipes to close. A grandchild that
inherited those pipes keeps them open, so the read blocks forever -- after the
timeout has already "fired".

Observed live: the daemon wedged with a worker thread in `do_poll` and no
backend process alive at all. It held its tick for 42 minutes, which stalled
every other lab, and the expired lease could never be reclaimed because
reclaim runs at the top of a tick and the tick was the thing stuck. A provider
timeout does not save you here; it is what triggers the hang.

`run_process` puts each child in its own session, so a timeout can kill the
whole process group and no descendant survives to hold a pipe.
"""
from __future__ import annotations

import os
import signal
import subprocess

# How long to wait for pipes to drain after killing the group. If descendants
# still hold them after this, we give up on the output rather than the daemon.
_DRAIN_SECONDS = 10


def run_process(
    cmd,
    *,
    capture_output: bool = False,
    text: bool = False,
    timeout: float | None = None,
    input=None,
    env=None,
    cwd=None,
    stdin=None,
    **_ignored,
) -> subprocess.CompletedProcess:
    """Drop-in for `subprocess.run` that group-kills on timeout."""
    stdout = subprocess.PIPE if capture_output else None
    stderr = subprocess.PIPE if capture_output else None
    if input is not None:
        stdin_arg = subprocess.PIPE
    else:
        stdin_arg = stdin if stdin is not None else subprocess.DEVNULL

    proc = subprocess.Popen(
        cmd,
        stdout=stdout,
        stderr=stderr,
        stdin=stdin_arg,
        text=text,
        env=env,
        cwd=cwd,
        start_new_session=True,   # own session => own process group to kill
    )
    try:
        out, err = proc.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        try:
            out, err = proc.communicate(timeout=_DRAIN_SECONDS)
        except subprocess.TimeoutExpired:
            # A descendant still holds the pipes. Abandon the output; the
            # daemon keeping its tick matters more than these bytes.
            proc.kill()
            out, err = None, None
        raise subprocess.TimeoutExpired(cmd, timeout, output=out, stderr=err)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child's whole process group, then the child itself."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass
