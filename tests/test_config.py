"""Tests for lab policy config (autoprof/config.py)."""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from autoprof import config  # noqa: E402


def _toml(tmp: str, body: str) -> Path:
    path = Path(tmp) / "autoprof.toml"
    path.write_text(body)
    return path


class MaxAcceptedPapersTests(unittest.TestCase):
    def test_default_is_four(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "absent.toml"
            self.assertEqual(config.max_accepted_papers(missing, env={}), 4)

    def test_reads_the_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _toml(tmp, "[lab]\nmax_accepted_papers = 7\n")
            self.assertEqual(config.max_accepted_papers(path, env={}), 7)

    def test_env_var_overrides_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _toml(tmp, "[lab]\nmax_accepted_papers = 7\n")
            self.assertEqual(
                config.max_accepted_papers(path, env={"AUTOPROF_MAX_ACCEPTED_PAPERS": "2"}), 2
            )

    def test_missing_lab_section_falls_back_to_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _toml(tmp, "[backends.default]\ngeneration = 'codex'\n")
            self.assertEqual(config.max_accepted_papers(path, env={}), 4)

    def test_garbage_values_fall_back_rather_than_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _toml(tmp, "[lab]\nmax_accepted_papers = 'lots'\n")
            self.assertEqual(config.max_accepted_papers(path, env={}), 4)
            self.assertEqual(
                config.max_accepted_papers(path, env={"AUTOPROF_MAX_ACCEPTED_PAPERS": "x"}), 4
            )

    def test_values_below_one_are_clamped(self):
        """A target of zero would disable the revise loop entirely, which
        is never what someone editing this setting means."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _toml(tmp, "[lab]\nmax_accepted_papers = 0\n")
            self.assertEqual(config.max_accepted_papers(path, env={}), 1)

    def test_shipped_config_declares_four(self):
        self.assertEqual(config.max_accepted_papers(env={}), 4)


if __name__ == "__main__":
    unittest.main()
