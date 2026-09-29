import os
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from pit import autopilot as A, bag, lanes, ledger as L, book as B

LANES = """
[tokens]
uncached_per_m = 0.24
cache_read_per_m = 0.05
out_per_m = 2.20
[reflect]
rows = 100000
[lanes.gpu-small]
usd_per_h = 14
slots = 1
gate = "true"
[pit]
enabled = true
default_stake = 0.25
[autopilot]
max_usd_per_hour = 40
[bag.gpu-small]
specs = ["bag/*.toml"]
max_per_day = 2
budget_usd = 2
budget_s = 300
house_stake = 0.25
"""


def bag_spec(sid, verdict="pass", extra=""):
    return (f'id = "{sid}"\nquestion = "{sid} holds?"\nexpect = "pass"\nif_pass = "ok"\nif_fail = "bisect"\nlane = "gpu-small"\n'
            f'budget_s = 5\nbudget_usd = 9\nrun = "echo \'pit: verdict={verdict}\'"\n{extra}')


class Bag(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "lanes.toml").write_text(LANES)
        (self.root / "ledger").mkdir()
        (self.root / "bag").mkdir()
        os.environ["PIT_HOST"] = "t"
        self.cfg = lanes.load(self.root)
        self.lg = L.Ledger(self.root / "ledger", "t")
        self.lg.append({"t": "settle", "job": "old", "variant": "main", "outcome": "fail", "pot": 5, "vig": 5, "totals": {}, "payouts": {"house": 5.0}})

    def spec(self, sid, **kw):
        (self.root / "bag" / f"{sid}.toml").write_text(bag_spec(sid, **kw))

    def ap(self):
        return A.Autopilot(self.root, self.lg, self.cfg, echo=lambda *_: None)

    def tick(self):
        ap = self.ap()
        ap.tick()
        ap.wait()

    def nodes(self):
        return bag.bag_jobs(self.lg.rows())

    def test_idle_lane_draws_posts_as_house_stakes_and_runs(self):
        self.spec("a")
        self.tick()
        (n,) = self.nodes()
        s = n["spec"]
        self.assertEqual((s["proposer"], s["bag"], s["bag_spec"], s["budget_usd"], s["budget_s"]), ("house", True, "a", 2, 300))
        (b,) = [r for r in self.lg.rows() if r["t"] == "bet"]
        self.assertEqual((b["agent"], b["book"], b["side"], b["usd"], b["tags"], b["job"]), ("house", "house", "pass", 0.25, ["bag"], n["id"]))
        st = L.fold(self.lg.rows())
        self.assertEqual(st.jobs[n["id"]]["result"]["verdict"], "pass")
        d = [r for r in self.lg.rows() if r["t"] == "auto" and r["type"] == "dispatch"]
        self.assertEqual((d[0]["agent"], d[0]["job"]), ("house", n["id"]))
        book = B.Book(self.lg.rows())
        self.assertEqual(book.proposers[n["id"]], "house")
        self.assertNotIn("house", B.calibration(self.lg.rows()).split()[7:])             # not a forecast
        # the budget debits no wallet: the house holds its pool plus the pot the stake came back into (no bettors: it keeps it)
        self.assertGreaterEqual(book.flows["house"], 5.0)

    def test_nothing_drawn_when_something_is_runnable(self):
        self.spec("a")
        self.lg.append({"t": "node", "kind": "job", "id": "real", "spec": {**__import__("tests.test_pit", fromlist=["job"]).job("real", run="echo 'pit: verdict=pass'")}})
        self.tick()
        self.assertEqual(self.nodes(), [])
        self.assertEqual(L.fold(self.lg.rows()).jobs["real"]["state"], "done")

    def test_per_day_cap(self):
        for s in ("a", "b", "c"):
            self.spec(s)
        for _ in range(4):
            self.tick()
        self.assertEqual(len(self.nodes()), 2)                                             # max_per_day = 2
        self.assertEqual(bag.today(self.lg.rows(), "gpu-small", datetime.now(timezone.utc)), 2)

    def test_spec_own_cap_and_one_at_a_time(self):
        self.spec("only", extra="max_per_day = 1\n")
        self.tick()
        self.tick()
        self.assertEqual(len(self.nodes()), 1)

    def test_fail_makes_a_regression_finding_naming_the_previous_pass(self):
        self.spec("a", verdict="fail")
        old = {"t": "node", "kind": "job", "id": "bag-a-old", "spec": {**bag_spec_dict("a"), "id": "bag-a-old"}}
        self.lg.append(old, "2026-09-28T01:00:00Z")
        self.lg.append({"t": "claim", "job": "bag-a-old", "lane": "gpu-small", "cid": "t:1"}, "2026-09-28T01:01:00Z")
        self.lg.append({"t": "result", "job": "bag-a-old", "verdict": "pass", "cost": {"usd": 0, "lane": "gpu-small", "wall_s": 1}}, "2026-09-28T01:05:00Z")
        self.tick()
        self.tick()                                   # the next tick turns the failed result into a finding
        st = L.fold(self.lg.rows())
        (f,) = st.findings.values()
        self.assertTrue(f["text"].startswith("REGRESSION: a failed at "))
        self.assertIn("previous pass 2026-09-28T01:05:00Z", f["text"])
        self.assertEqual(len(L.fold(self.lg.rows()).findings), 1)                          # once
        digest = B.digest(self.lg.rows(), list(range(len(self.lg.rows()))))
        self.assertIn("[bag]", digest)                                                     # agents see it as a bag market

    def test_least_recently_run_first(self):
        for s in ("a", "b", "c"):
            self.spec(s)
        for sid, ts in (("a", "2026-09-28T03:00:00Z"), ("b", "2026-09-28T01:00:00Z")):     # c never ran
            jid = f"bag-{sid}-x"
            self.lg.append({"t": "node", "kind": "job", "id": jid, "spec": {**bag_spec_dict(sid), "id": jid}}, ts)
            self.lg.append({"t": "result", "job": jid, "verdict": "pass", "cost": {"usd": 0, "lane": "gpu-small", "wall_s": 1}}, ts)
        order = []
        now, c = datetime.now(timezone.utc), bag.conf(self.cfg, "gpu-small")
        for _ in range(3):
            s, last = bag.draw(self.root, self.lg.rows(), self.cfg, "gpu-small", c, now)
            order.append((s["id"], last))
            jid = f"bag-{s['id']}-y{len(order)}"
            self.lg.append({"t": "node", "kind": "job", "id": jid, "spec": {**bag_spec_dict(s["id"]), "id": jid}})
            self.lg.append({"t": "result", "job": jid, "verdict": "pass", "cost": {"usd": 0, "lane": "gpu-small", "wall_s": 1}})
        self.assertEqual([o[0] for o in order], ["c", "b", "a"])                           # never run, then oldest
        self.assertIsNone(order[0][1])


def bag_spec_dict(sid):
    return {**__import__("pit.spec", fromlist=["loads"]).loads(bag_spec(sid)), "lane": "gpu-small", "proposer": "house", "bag": True, "bag_spec": sid}


class Examples(unittest.TestCase):
    def test_shipped_example_specs_validate(self):
        from pit import spec as specmod
        root = Path(__file__).resolve().parent.parent / "examples" / "replay-synthetic"
        cfg = lanes.load(root)
        for lane in ("gpu-small", "gpu-large", "ci"):
            c = bag.conf(cfg, lane)
            fs = bag.files(root, c)
            self.assertEqual(len(fs), 2)
            for p in fs:
                s = specmod.load(p)
                self.assertEqual(specmod.validate({**s, "lane": lane}, cfg["lanes"]), [])
