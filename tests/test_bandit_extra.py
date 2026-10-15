"""Run: python -m unittest discover tests"""
import copy
import os
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def _app(cid, hook="h_one", genes=("g1", "g2"), cluster="ba_pa", dna="d1", outcome="PENDING"):
    from anvil import bandit
    return {"canonical_id": cid, "company": "Co", "role": "R", "cluster": cluster, "dna_hash": dna,
            "hook_id": hook, "genes_used": list(genes), "recipe": {"summary": "s"}, "applied_at": bandit.now(),
            "outcome": outcome}


class BanditBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        os.environ["ANVIL_HOME"] = str(self.tmp)
        from anvil import bandit
        self.bandit = bandit

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("ANVIL_HOME", None)

    def arm(self, gene, cluster="ba_pa"):
        return self.bandit.load_log()["genes"].get(cluster, {}).get(gene, {"alpha": 1, "beta": 1})

    def hook(self, hook="h_one", cluster="ba_pa"):
        return self.bandit.load_log()["hooks"][cluster][hook]


class TestOutcomeCrediting(BanditBase):
    def test_first_positive_credits_once(self):
        b = self.bandit
        b.record_application(_app("c1"))
        b.apply_outcome("c1", "callback")
        self.assertEqual(self.arm("g1")["alpha"], 3)
        self.assertEqual(self.hook()["positive"], 1)
        self.assertTrue(b.load_log()["golden"]["ba_pa"])
        # Interview after callback: outcome text moves on, no more alpha, no second golden or hook credit.
        app = b.apply_outcome("c1", "interview")
        self.assertEqual(app["outcome"], "INTERVIEW")
        self.assertEqual(app["credited"], "positive")
        self.assertEqual(self.arm("g1")["alpha"], 3)
        self.assertEqual(self.arm("g2")["alpha"], 3)
        self.assertEqual(self.hook()["positive"], 1)
        self.assertEqual(len(b.load_log()["golden"]["ba_pa"]), 1)

    def test_rejection_after_positive_keeps_credit(self):
        b = self.bandit
        b.record_application(_app("c1"))
        b.apply_outcome("c1", "callback")
        b.apply_outcome("c1", "rejected")
        self.assertEqual(self.arm("g1")["beta"], 1)
        self.assertEqual(self.arm("g1")["alpha"], 3)
        self.assertEqual(b.load_apps()[-1]["credited"], "positive")

    def test_negative_on_uncredited_app_adds_one_beta(self):
        b = self.bandit
        b.record_application(_app("c1"))
        app = b.apply_outcome("c1", "rejected")
        self.assertEqual(self.arm("g1")["beta"], 2)
        self.assertEqual(self.arm("g2")["beta"], 2)
        self.assertEqual(app["credited"], "negative")

    def test_second_negative_adds_nothing(self):
        b = self.bandit
        b.record_application(_app("c1"))
        b.apply_outcome("c1", "ghosted")
        b.apply_outcome("c1", "rejected")
        self.assertEqual(self.arm("g1")["beta"], 2)

    def test_late_positive_after_ghost_undoes_beta_then_credits(self):
        b = self.bandit
        b.record_application(_app("c1"))
        b.apply_outcome("c1", "ghosted")
        self.assertEqual(self.arm("g1")["beta"], 2)
        app = b.apply_outcome("c1", "callback")
        self.assertEqual(self.arm("g1")["beta"], 1)   # undone, not below 1
        self.assertEqual(self.arm("g1")["alpha"], 3)  # then the positive credit
        self.assertEqual(app["credited"], "positive")
        self.assertEqual(self.hook()["positive"], 1)

    def test_undo_never_drops_beta_below_one(self):
        b = self.bandit
        log = b.load_log()
        log["genes"].setdefault("ba_pa", {})["g1"] = {"alpha": 1, "beta": 1}
        b.save_log(log)
        b.record_application(_app("c1", genes=("g1",)))
        b.apply_outcome("c1", "ghosted")
        log = b.load_log()
        log["genes"]["ba_pa"]["g1"]["beta"] = 1  # simulate a beta that was already reset
        b.save_log(log)
        b.apply_outcome("c1", "offer")
        self.assertEqual(self.arm("g1")["beta"], 1)

    def test_same_outcome_twice_changes_nothing(self):
        b = self.bandit
        b.record_application(_app("c1"))
        b.apply_outcome("c1", "ghosted")
        before = b.load_log()
        b.apply_outcome("c1", "ghosted")
        self.assertEqual(b.load_log()["genes"], before["genes"])
        self.assertEqual(self.arm("g1")["beta"], 2)
        b.apply_outcome("c1", "callback")
        alpha = self.arm("g1")["alpha"]
        b.apply_outcome("c1", "callback")
        self.assertEqual(self.arm("g1")["alpha"], alpha)
        self.assertEqual(self.hook()["positive"], 1)

    def test_legacy_row_without_flag_is_inferred(self):
        b = self.bandit
        apps = [_app("old", outcome="CALLBACK")]
        b.save_apps(apps)
        b.apply_outcome("old", "interview")
        self.assertEqual(self.arm("g1")["alpha"], 1)  # no extra credit for a legacy callback


class TestDeprecation(BanditBase):
    def _send(self, n, hook="h_one"):
        for i in range(n):
            self.bandit.record_application(_app(f"c{i}", hook=hook))

    def test_eight_ghosts_deprecate(self):
        self._send(8)
        for i in range(8):
            self.bandit.apply_outcome(f"c{i}", "ghosted")
        self.assertTrue(self.bandit.is_deprecated(self.bandit.load_log(), "ba_pa", "h_one"))

    def test_eight_sends_with_one_rejection_is_not_deprecated(self):
        self._send(8)
        for i in range(7):
            self.bandit.apply_outcome(f"c{i}", "ghosted")
        self.bandit.apply_outcome("c7", "rejected")
        self.assertFalse(self.bandit.is_deprecated(self.bandit.load_log(), "ba_pa", "h_one"))

    def test_seven_sends_are_not_deprecated(self):
        self._send(7)
        self.assertFalse(self.bandit.is_deprecated(self.bandit.load_log(), "ba_pa", "h_one"))

    def test_reply_lifts_existing_deprecation(self):
        self._send(8)
        for i in range(8):
            self.bandit.apply_outcome(f"c{i}", "ghosted")
        self.bandit.apply_outcome("c0", "callback")
        self.assertFalse(self.bandit.is_deprecated(self.bandit.load_log(), "ba_pa", "h_one"))


class TestRetailoring(BanditBase):
    def test_retailor_pending_same_hook_does_not_resend(self):
        b = self.bandit
        b.record_application(_app("c1"))
        b.record_application(_app("c1"))
        self.assertEqual(self.hook()["sent"], 1)
        self.assertEqual(len(b.load_apps()), 1)

    def test_retailor_pending_new_hook_moves_the_send(self):
        b = self.bandit
        b.record_application(_app("c1", hook="h_one"))
        b.record_application(_app("c1", hook="h_two"))
        self.assertEqual(self.hook("h_one")["sent"], 0)
        self.assertEqual(self.hook("h_two")["sent"], 1)

    def test_new_canonical_id_is_a_send(self):
        b = self.bandit
        b.record_application(_app("c1"))
        b.record_application(_app("c2"))
        self.assertEqual(self.hook()["sent"], 2)

    def test_retailor_after_reply_does_not_resend(self):
        b = self.bandit
        b.record_application(_app("c1"))
        b.apply_outcome("c1", "callback")
        b.record_application(_app("c1"))
        self.assertEqual(self.hook()["sent"], 1)


class TestEngineFixes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(ROOT / "vault", self.tmp / "vault",
                        ignore=shutil.ignore_patterns("gene-pool.json", "overrides.json"))
        os.environ["ANVIL_HOME"] = str(self.tmp)
        from anvil import ingest, store
        self.store = store
        self.pool = ingest.run()
        self.sk = store.skeleton()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.environ.pop("ANVIL_HOME", None)

    def _bullets(self, section):
        return [g for g, x in self.pool["genes"].items() if x["kind"] == "bullet" and x["section"] == section]

    def _choose(self, sk=None, golden=None, seed=7):
        from anvil import bandit, engine
        return engine.choose(self.pool, sk or self.sk, "ba_pa", {}, bandit.load_log(),
                             random.Random(seed), golden=golden)

    def test_golden_replay_tops_up_to_min(self):
        fend = self._bullets("fendahl")
        self.assertGreaterEqual(len(fend), 6)
        gone = fend[1:3]  # 7 - 2 = 5 live, exactly the minimum
        for gid in gone:
            self.pool["genes"][gid]["blocked"] = True  # these left the pool since the golden was saved
        golden = {"recipe": {"bullets": {"fendahl": [fend[0]] + gone}}}
        plan = self._choose(golden=golden)
        got = plan.recipe["bullets"]["fendahl"]
        mn = next(e["min_bullets"] for e in self.sk["experience"] if e["key"] == "fendahl")
        self.assertGreaterEqual(len(got), mn)
        self.assertEqual(len(got), len(set(got)))
        self.assertFalse(set(got) & set(gone))
        self.assertEqual(got[0], fend[0])

    def test_golden_replay_truncates_to_max(self):
        sk = copy.deepcopy(self.sk)
        for e in sk["experience"]:
            if e["key"] == "fendahl":
                e["max_bullets"] = 5
        fend = self._bullets("fendahl")
        golden = {"recipe": {"bullets": {"fendahl": fend}}}
        plan = self._choose(sk=sk, golden=golden)
        self.assertEqual(len(plan.recipe["bullets"]["fendahl"]), 5)

    def test_bounds_are_asserted(self):
        sk = copy.deepcopy(self.sk)
        for e in sk["experience"]:
            if e["key"] == "fendahl":
                e["min_bullets"] = 50  # impossible with 7 bullets in the pool
        with self.assertRaises(AssertionError):
            self._choose(sk=sk)

    def test_failed_deprecation_escape_restores_original_recipe(self):
        base = self._choose(seed=3).recipe
        with mock.patch("anvil.bandit.is_deprecated", return_value=True):
            plan = self._choose(seed=3)
        self.assertEqual(plan.recipe["summary"], base["summary"])
        self.assertEqual(plan.recipe["bullets"]["fendahl"], base["bullets"]["fendahl"])


if __name__ == "__main__":
    unittest.main()
