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
