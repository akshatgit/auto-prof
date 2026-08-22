import os
import signal
import subprocess
import sys
import time
import unittest

from autoprof.backends.process import run_process


class RunProcessTests(unittest.TestCase):
    """A timeout must end the work, not just the direct child."""

    def test_normal_run_returns_output(self):
        r = run_process([sys.executable, "-c", "print('hi')"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn("hi", r.stdout)

    def test_nonzero_exit_is_reported(self):
        r = run_process([sys.executable, "-c", "raise SystemExit(3)"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)

    def test_input_is_delivered_on_stdin(self):
        r = run_process([sys.executable, "-c", "import sys;print(sys.stdin.read().strip())"],
                        capture_output=True, text=True, input="fed-in")
        self.assertIn("fed-in", r.stdout)

    def test_timeout_raises(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_process([sys.executable, "-c", "import time;time.sleep(30)"],
                        capture_output=True, text=True, timeout=1)

    def test_timeout_kills_a_surviving_grandchild(self):
        """The exact production hang: a grandchild holding the stdout pipe.

        With subprocess.run this blocks forever after the timeout because the
        pipe never closes. The whole group must die.
        """
        script = (
            "import subprocess,sys,time\n"
            # grandchild inherits stdout and outlives the direct child
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)'])\n"
            "time.sleep(120)\n"
        )
        start = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_process([sys.executable, "-c", script],
                        capture_output=True, text=True, timeout=2)
        elapsed = time.time() - start
        # Must not hang for the grandchild's full 120s sleep.
        self.assertLess(elapsed, 30, "timeout did not terminate the process group")

    def test_child_runs_in_its_own_session(self):
        r = run_process([sys.executable, "-c", "import os;print(os.getpid()==os.getsid(0))"],
                        capture_output=True, text=True)
        self.assertIn("True", r.stdout)

    def test_env_and_cwd_are_honoured(self):
        r = run_process([sys.executable, "-c", "import os;print(os.environ.get('MARK'),os.getcwd())"],
                        capture_output=True, text=True, env={"MARK": "yes", "PATH": os.environ["PATH"]},
                        cwd="/tmp")
        self.assertIn("yes", r.stdout)
        self.assertIn("/tmp", r.stdout)


class IdleTimeoutTests(unittest.TestCase):
    """A working job must survive; a silent one must not."""

    def test_steady_output_survives_far_past_the_idle_window(self):
        # 6s of work with output every 0.2s, idle window 2s. A wall-clock
        # kill at 2s would murder healthy work; an idle timeout must not.
        script = ("import sys,time\n"
                  "for i in range(30):\n"
                  "    print('tick',i,flush=True)\n"
                  "    time.sleep(0.2)\n")
        start = time.time()
        r = run_process([sys.executable, "-c", script], capture_output=True,
                        text=True, idle_timeout=2)
        self.assertEqual(r.returncode, 0)
        self.assertIn("tick 29", r.stdout)
        self.assertGreater(time.time() - start, 4)

    def test_a_silent_child_is_killed(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_process([sys.executable, "-c", "import time;time.sleep(60)"],
                        capture_output=True, text=True, idle_timeout=2)

    def test_output_then_silence_is_killed(self):
        script = "import time;print('started',flush=True);time.sleep(60)\n"
        start = time.time()
        with self.assertRaises(subprocess.TimeoutExpired) as ctx:
            run_process([sys.executable, "-c", script], capture_output=True,
                        text=True, idle_timeout=2)
        self.assertLess(time.time() - start, 20)
        self.assertIn("started", ctx.exception.output or "")

    def test_progress_callback_sees_work(self):
        seen = []
        script = "import time\nfor i in range(3):\n print('line',i,flush=True)\n time.sleep(0.1)\n"
        run_process([sys.executable, "-c", script], capture_output=True, text=True,
                    idle_timeout=5, on_progress=lambda s, c, n: seen.append((s, n)))
        self.assertGreaterEqual(len(seen), 3)
        self.assertTrue(all(s == "stdout" for s, _ in seen))
        self.assertEqual(seen[-1][1], max(n for _, n in seen))  # bytes accumulate

    def test_a_broken_progress_callback_cannot_kill_the_job(self):
        def boom(stream, chunk, total):
            raise RuntimeError("reporting bug")
        r = run_process([sys.executable, "-c", "print('fine')"], capture_output=True,
                        text=True, idle_timeout=5, on_progress=boom)
        self.assertEqual(r.returncode, 0)
        self.assertIn("fine", r.stdout)

    def test_hard_timeout_still_applies_when_set(self):
        script = "import time\nwhile True:\n print('busy',flush=True)\n time.sleep(0.1)\n"
        start = time.time()
        with self.assertRaises(subprocess.TimeoutExpired):
            run_process([sys.executable, "-c", script], capture_output=True,
                        text=True, timeout=3, idle_timeout=60)
        self.assertLess(time.time() - start, 20)
