import unittest
from unittest import mock
import os

from autoprof import usage
from tests.helpers import fresh_db, seed_lab_with_student


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.conn = fresh_db()
        self.ids = seed_lab_with_student(self.conn)

    def tearDown(self):
        self.conn.close()

    def _job(self, model="gpt-5.5", produced=0, inp=0, cached=0, progressed=True):
        cur = self.conn.execute(
            "INSERT INTO jobs (kind,target_type,target_id,status,backend,backend_model,"
            "progress_at,progress_tokens,progress_input_tokens,progress_cached_tokens) "
            "VALUES ('student_work','task',?, 'done','codex',?,?,?,?,?)",
            (self.ids["task_id"], model,
             "2026-08-22 10:00:00" if progressed else None, produced, inp, cached))
        self.conn.commit()
        return cur.lastrowid

    def _sample(self, job_id, produced, minutes_ago, inp=0):
        self.conn.execute(
            "INSERT INTO token_samples (job_id,produced_tokens,input_tokens,sampled_at) "
            "VALUES (?,?,?, datetime('now', ?))",
            (job_id, produced, inp, f"-{minutes_ago} minutes"))
        self.conn.commit()

    def test_totals_sum_across_jobs(self):
        self._job(produced=100, inp=1000, cached=800)
        self._job(produced=250, inp=2000, cached=1500)
        t = usage.totals(self.conn)
        self.assertEqual(t["produced"], 350)
        self.assertEqual(t["input"], 3000)
        self.assertEqual(t["cached"], 2300)
        self.assertEqual(t["jobs"], 2)

    def test_jobs_without_progress_are_excluded(self):
        self._job(produced=100, progressed=False)
        self.assertEqual(usage.totals(self.conn)["jobs"], 0)

    def test_rate_uses_the_window_only(self):
        j = self._job()
        self._sample(j, 1000, 60)   # before the window
        self._sample(j, 1200, 10)   # inside
        self._sample(j, 1500, 1)    # inside
        r = usage.rate(self.conn, minutes=20)
        self.assertEqual(r["produced"], 300)      # 1500-1200, not 1500-1000
        self.assertAlmostEqual(r["per_hour"], 900.0)

    def test_rate_is_zero_when_nothing_ran(self):
        self.assertEqual(usage.rate(self.conn, minutes=20)["produced"], 0)

    def test_two_jobs_do_not_cross_contaminate(self):
        a, b = self._job(), self._job()
        self._sample(a, 100, 5); self._sample(a, 300, 1)
        self._sample(b, 5000, 5); self._sample(b, 5100, 1)
        self.assertEqual(usage.rate(self.conn, minutes=20)["produced"], 300)

    def test_cost_is_unavailable_without_prices(self):
        self._job(produced=1_000_000)
        with mock.patch.dict(os.environ, {}, clear=True):
            c = usage.cost(self.conn)
        self.assertEqual(c["total"], 0.0)
        self.assertIn("gpt-5.5", c["unpriced"])

    def test_cost_uses_configured_prices(self):
        self._job(produced=1_000_000, inp=2_000_000, cached=1_000_000)
        env = {"AUTOPROF_PRICE_GPT_5_5_OUTPUT": "10",
               "AUTOPROF_PRICE_GPT_5_5_INPUT": "1",
               "AUTOPROF_PRICE_GPT_5_5_CACHED": "0.1"}
        c = usage.cost(self.conn, env=env)
        # 1M uncached input @1 + 1M cached @0.1 + 1M output @10
        self.assertAlmostEqual(c["total"], 1.0 + 0.1 + 10.0)
        self.assertEqual(c["unpriced"], [])

    def test_partial_prices_still_estimate(self):
        self._job(produced=1_000_000, inp=1_000_000)
        c = usage.cost(self.conn, env={"AUTOPROF_PRICE_GPT_5_5_OUTPUT": "5"})
        self.assertAlmostEqual(c["total"], 5.0)

    def test_models_are_reported_separately(self):
        self._job(model="gpt-5.5", produced=1_000_000)
        self._job(model="minimax-m3", produced=1_000_000)
        c = usage.cost(self.conn, env={"AUTOPROF_PRICE_GPT_5_5_OUTPUT": "10"})
        self.assertEqual([m["model"] for m in c["by_model"]], ["gpt-5.5"])
        self.assertIn("minimax-m3", c["unpriced"])

    def test_jobs_with_no_recorded_backend_are_named_not_dropped(self):
        self.conn.execute(
            "INSERT INTO jobs (kind,target_type,target_id,status,progress_at,progress_tokens) "
            "VALUES ('student_work','task',?, 'done','2026-08-22 10:00:00', 500)",
            (self.ids["task_id"],))
        self.conn.commit()
        c = usage.cost(self.conn, env={})
        self.assertIn("(unrecorded backend)", c["unpriced"])
        self.assertTrue(all(isinstance(m, str) for m in c["unpriced"]))
