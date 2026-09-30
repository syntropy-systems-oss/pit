import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pit import autopilot as A, bag, lanes, ledger as L, book as B

LANES = """
[prices]
usd_per_mtok_in = 0.24
usd_per_mtok_cached = 0.05
usd_per_mtok_out = 2.20
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
house_stake = 0.25
"""


def bag_spec(sid, verdict="pass", extra=""):
    return (f'id = "{sid}"\nquestion = "{sid} holds?"\nexpect = "pass"\nif_pass = "ok"\nif_fail = "bisect"\nlane = "gpu-small"\n'
            f'budget_usd = 9\nrun = "echo \'pit: verdict={verdict}\'"\n{extra}')


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
        self.assertEqual((s["proposer"], s["bag"], s["bag_spec"], s["budget_usd"]), ("house", True, "a", 9))      # the spec's own budget beats the lane's fallback
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

    def test_why_blocked_names_the_cap(self):
        import contextlib, io, types
        from unittest import mock
        from pit import cli
        for s in ("a", "b"):
            self.spec(s)
        for _ in range(3):
            self.tick()
        node = dict(self.nodes()[0], id="queued-desk")
        node["spec"] = dict(node["spec"], id="queued-desk", bag=False)
        self.lg.append(node)
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(cli, "ctx", lambda: (self.root, self.lg, self.cfg)):
            cli.cmd_why(types.SimpleNamespace(id="queued-desk"))
        self.assertIn("daily cap reached, resets", out.getvalue())

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

    def result(self, jid, verdict, ts):
        self.lg.append({"t": "result", "job": jid, "verdict": verdict, "cost": {"usd": 0, "lane": "gpu-small", "wall_s": 1}}, ts)

    def test_preflight_failure_refuses_once_per_hour_and_draws_nothing(self):
        self.spec("a", extra='preflight = "echo images missing; exit 1"\n')
        for _ in range(3):
            self.tick()
        self.assertEqual(self.nodes(), [])
        refs = [r for r in self.lg.rows() if r["t"] == "auto" and r["type"] == "refuse"]
        self.assertEqual([r["reason"] for r in refs], ["bag gpu-small preflight: images missing"])

    def test_preflight_success_draws(self):
        self.spec("a", extra='preflight = "true"\n')
        self.tick()
        self.assertEqual(len(self.nodes()), 1)

    def test_invalid_backs_off_with_doubling_fail_does_not(self):
        self.spec("a", verdict="invalid")
        c = bag.conf(self.cfg, "gpu-small")
        self.tick()
        (j1,) = [n["id"] for n in self.nodes()]
        r = next(r for r in self.lg.rows() if r["t"] == "result")
        self.assertEqual(r["verdict"], "invalid")
        until, k = bag.backoff_until(self.lg.rows(), "gpu-small", c)
        self.assertEqual(k, 1)
        self.tick()
        self.assertEqual(len(self.nodes()), 1)                                              # inside the 30 min: no draw
        n2 = "bag-a-x2"                                                                      # a second consecutive invalid: doubles
        self.lg.append({"t": "node", "kind": "job", "id": n2, "spec": {**bag_spec_dict("a"), "id": n2}})
        self.result(n2, "invalid", None)
        until, k = bag.backoff_until(self.lg.rows(), "gpu-small", c)
        last = max(r["ts"] for r in self.lg.rows() if r["t"] == "result")
        self.assertEqual((k, until - datetime.fromisoformat(last)), (2, timedelta(minutes=60)))
        n3 = "bag-a-x3"
        self.lg.append({"t": "node", "kind": "job", "id": n3, "spec": {**bag_spec_dict("a"), "id": n3}})
        self.result(n3, "fail", None)
        self.assertIsNone(bag.backoff_until(self.lg.rows(), "gpu-small", c))              # a fail is signal: normal

    def test_invalid_draw_does_not_count_toward_the_day(self):
        self.spec("a", verdict="invalid")
        self.tick()
        self.assertEqual(len(self.nodes()), 1)
        self.assertEqual(bag.today(self.lg.rows(), "gpu-small", datetime.now(timezone.utc)), 0)

    def test_bag_reset_note_clears_the_backoff(self):
        self.spec("a", verdict="invalid")
        self.tick()
        c = bag.conf(self.cfg, "gpu-small")
        self.assertIsNotNone(bag.backoff_until(self.lg.rows(), "gpu-small", c))
        self.lg.append({"t": "auto", "type": "note", "reason": "bag-reset gpu-small"})
        self.assertIsNone(bag.backoff_until(self.lg.rows(), "gpu-small", c))

    def test_min_interval_between_draws_of_a_spec(self):
        self.spec("a")
        self.tick()
        self.tick()
        self.assertEqual(len(self.nodes()), 1)                  # ran minutes ago: not drawn again (default 120 min)
        self.cfg["bag"]["min_interval_minutes"] = 0             # a [bag] scalar reaches every lane
        self.tick()
        self.assertEqual(len(self.nodes()), 2)

    def test_bag_scalars_apply_and_lane_keys_win(self):
        cfg = {"bag": {"quarantine_after": 5, "min_interval_minutes": 10, "l": {"quarantine_after": 2}}}
        c = bag.conf(cfg, "l")
        self.assertEqual((c["quarantine_after"], c["min_interval_minutes"], c["backoff_minutes"]), (2, 10, 30))

    def test_quarantined_spec_is_not_drawn(self):
        self.spec("a", verdict="fail")
        self.cfg["bag"].update(min_interval_minutes=0, quarantine_after=1)
        self.cfg["bag"]["gpu-small"]["max_per_day"] = 9
        self.tick()
        self.tick()
        self.assertEqual(len(self.nodes()), 1)

    def test_bag_invalid_wakes_nobody(self):
        self.spec("a", verdict="invalid")
        self.tick()
        rows = self.lg.rows()
        book = B.Book(rows)
        self.assertEqual(B.board_events(rows, book, {"alice"}, 1), [])          # row 0 is the seed settle
        self.assertFalse([r for r in rows if r["t"] == "auto" and r["type"] in ("handback", "wake")])

    def test_bag_fail_is_an_event_pass_too_but_finding_is_the_signal(self):
        self.spec("a", verdict="fail")
        self.tick()
        rows = self.lg.rows()
        ev = [rows[i]["t"] for i in B.board_events(rows, B.Book(rows), {"alice"}, 0)]
        self.assertIn("result", ev)
        self.assertNotIn("node", ev)                                                        # the bag post is not

    def test_reflect_rows_ignore_junk(self):
        from pit import reflect
        self.spec("a", verdict="invalid")
        self.tick()
        rows = self.lg.rows()
        self.assertLess(len(reflect.counted(rows)), len(rows))
        self.assertEqual([r["t"] for r in reflect.counted(rows)], [])                        # settle is not work
        self.assertIn("counted", reflect.why(rows, datetime.now(timezone.utc), {"reflect": {"rows": 5}}))


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



class Quarantine(unittest.TestCase):
    def test_three_consecutive_fails_leave_the_rotation(self):
        from pit import bag
        rows = [{"t": "node", "kind": "job", "id": f"bag-x-{i}", "ts": f"2026-01-01T0{i}:00:00Z", "spec": {"bag_spec": "x"}} for i in range(4)]
        rows += [{"t": "result", "job": f"bag-x-{i}", "verdict": v, "ts": f"2026-01-01T0{i}:30:00Z"} for i, v in enumerate(["pass", "fail", "fail", "fail"])]
        self.assertEqual(bag.consecutive_fails(rows, "x"), 3)
        rows.append({"t": "auto", "type": "note", "reason": "bag-readmit x", "ts": "2026-01-01T04:00:00Z"})
        self.assertEqual(bag.consecutive_fails(rows, "x"), 0)

    def test_invalids_quarantine_and_same_scenario_counts_as_last_run(self):
        rows = [{"t": "node", "kind": "job", "id": f"bag-x-{i}", "ts": f"2026-01-01T0{i}:00:00Z", "spec": {"bag": True, "lane": "l", "bag_spec": "x"}} for i in range(3)]
        rows += [{"t": "result", "job": f"bag-x-{i}", "verdict": "invalid", "ts": f"2026-01-01T0{i}:30:00Z"} for i in range(3)]
        self.assertEqual(bag.consecutive_fails(rows, "x"), 3)
        rows += [{"t": "node", "kind": "job", "id": "adhoc", "ts": "2026-01-02T00:00:00Z", "spec": {"scenario": "s", "lane": "l"}},   # not a bag job: any id counts
                 {"t": "result", "job": "adhoc", "verdict": "pass", "ts": "2026-01-02T00:30:00Z"}]
        self.assertEqual(bag.last_run(rows, "x", "s", "l"), "2026-01-02T00:30:00Z")
        self.assertEqual(bag.last_run(rows, "x", "s", "other"), "2026-01-01T02:30:00Z")
