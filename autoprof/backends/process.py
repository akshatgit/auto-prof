"""Subprocess execution whose timeout tracks WORK, not wall clock.

`subprocess.run(capture_output=True, timeout=N)` kills only the DIRECT child
and then waits for the stdout/stderr pipes to close. A grandchild that
inherited those pipes keeps them open, so the read blocks forever -- after the
timeout has already "fired". Observed live: the daemon wedged with a worker
thread in `do_poll` and no backend process alive at all, holding its tick for
42 minutes and stalling every other lab.

It also read nothing until the child exited, so a job that was working looked
identical to a job that had hung, and the only defence was a fixed wall-clock
kill that punished long-but-healthy work exactly as hard as a deadlock.

`run_process` streams the child's output as it arrives, reports progress to a
callback, and applies an IDLE timeout: a child that is still producing output
is still working and is left alone, while one that has gone silent is killed
along with its whole process group.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time

# Grace period for pipes to drain after the group is killed. If descendants
# still hold them, we abandon the output rather than the daemon.
_DRAIN_SECONDS = 10


def run_process(
    cmd,
    *,
    capture_output: bool = False,
    text: bool = False,
    timeout: float | None = None,
    idle_timeout: float | None = None,
    on_progress=None,
    input=None,
    env=None,
    cwd=None,
    stdin=None,
    **_ignored,
) -> subprocess.CompletedProcess:
    """Run `cmd`, streaming output.

    `timeout` is a hard wall-clock cap (None = none). `idle_timeout` kills the
    child only after that many seconds with NO output, so long healthy work
    survives. `on_progress(stream, chunk, total_bytes)` is called as output
    arrives; exceptions from it are swallowed so a reporting bug can never
    take down a research job.
    """
    stdout = subprocess.PIPE if capture_output else None
    stderr = subprocess.PIPE if capture_output else None
    stdin_arg = subprocess.PIPE if input is not None else (
        stdin if stdin is not None else subprocess.DEVNULL
    )

    proc = subprocess.Popen(
        cmd, stdout=stdout, stderr=stderr, stdin=stdin_arg,
        text=text, env=env, cwd=cwd,
        start_new_session=True,   # own session => a group we can kill whole
    )

    state = {"last": time.monotonic(), "bytes": 0}
    lock = threading.Lock()
    chunks: dict[str, list] = {"stdout": [], "stderr": []}

    def pump(name, handle):
        if handle is None:
            return
        for chunk in iter(lambda: handle.readline(), "" if text else b""):
            with lock:
                chunks[name].append(chunk)
                state["last"] = time.monotonic()
                state["bytes"] += len(chunk)
                total = state["bytes"]
            if on_progress is not None:
                try:
                    on_progress(name, chunk, total)
                except Exception:      # noqa: BLE001 -- reporting must not kill work
                    pass
        try:
            handle.close()
        except OSError:
            pass

    pumps = [
        threading.Thread(target=pump, args=(n, h), daemon=True)
        for n, h in (("stdout", proc.stdout), ("stderr", proc.stderr))
    ]
    for t in pumps:
        t.start()

    if input is not None and proc.stdin is not None:
        try:
            proc.stdin.write(input)
        except (BrokenPipeError, OSError):
            pass
        try:
            proc.stdin.close()
        except OSError:
            pass

    started = time.monotonic()
    timed_out = False
    while True:
        if proc.poll() is not None:
            break
        now = time.monotonic()
        if timeout is not None and now - started > timeout:
            timed_out = True
            break
        if idle_timeout is not None:
            with lock:
                quiet = now - state["last"]
            if quiet > idle_timeout:
                timed_out = True
                break
        time.sleep(0.25)

    if timed_out:
        _kill_group(proc)
        deadline = time.monotonic() + _DRAIN_SECONDS
        for t in pumps:
            t.join(timeout=max(0.0, deadline - time.monotonic()))
        out, err = _joined(chunks, text)
        raise subprocess.TimeoutExpired(cmd, timeout or idle_timeout, output=out, stderr=err)

    for t in pumps:
        t.join(timeout=_DRAIN_SECONDS)
    proc.wait()
    out, err = _joined(chunks, text)
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _joined(chunks, text):
    empty = "" if text else b""
    out = empty.join(chunks["stdout"]) if chunks["stdout"] else (empty if text else b"")
    err = empty.join(chunks["stderr"]) if chunks["stderr"] else (empty if text else b"")
    return out, err


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
