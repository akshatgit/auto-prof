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

    def test_lab_scoped_zero_means_unlimited_without_changing_global_default(self):
        env = {"AUTOPROF_MAX_ACCEPTED_PAPERS_9": "0"}
        self.assertEqual(config.max_accepted_papers(env=env, lab_id=9), 0)
        self.assertEqual(config.max_accepted_papers(env=env, lab_id=8), 4)

    def test_lab_scoped_environment_overrides_global_environment(self):
        env = {
            "AUTOPROF_MAX_SUPERVISION_ROUNDS": "3",
            "AUTOPROF_MAX_SUPERVISION_ROUNDS_9": "0",
        }
        self.assertEqual(config.max_supervision_rounds(env=env, lab_id=9), 0)
        self.assertEqual(config.max_supervision_rounds(env=env, lab_id=8), 3)

    def test_lab_scoped_toml_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _toml(
                tmp,
                "[lab]\nmax_review_exchanges = 2\n"
                "[labs.9]\nmax_review_exchanges = 0\n",
            )
            self.assertEqual(config.max_review_exchanges(path, env={}, lab_id=9), 0)
            self.assertEqual(config.max_review_exchanges(path, env={}, lab_id=8), 2)

    def test_scoped_zero_is_supported_for_every_research_loop(self):
        names_and_getters = [
            ("AUTOPROF_MAX_REJECTED_PAPERS_9", config.max_rejected_papers),
            ("AUTOPROF_MAX_REVIEW_EXCHANGES_9", config.max_review_exchanges),
            ("AUTOPROF_MAX_COLLABORATION_ROUNDS_9", config.max_collaboration_rounds),
            ("AUTOPROF_MAX_LAB_REVIEW_ROUNDS_9", config.max_lab_review_rounds),
            ("AUTOPROF_MAX_TOOL_ROUNDS_9", config.max_tool_rounds),
            ("AUTOPROF_MAX_PAPER_REVISION_ROUNDS_9", config.max_paper_revision_rounds),
        ]
        for name, getter in names_and_getters:
            with self.subTest(name=name):
                self.assertEqual(getter(env={name: "0"}, lab_id=9), 0)

    def test_paper_revision_rounds_default_and_lab_override(self):
        self.assertEqual(config.max_paper_revision_rounds(env={}), 3)
        env = {"AUTOPROF_MAX_PAPER_REVISION_ROUNDS_9": "1"}
        self.assertEqual(config.max_paper_revision_rounds(env=env, lab_id=9), 1)
        self.assertEqual(config.max_paper_revision_rounds(env=env, lab_id=8), 3)


if __name__ == "__main__":
    unittest.main()
