import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pit import autopilot as A, lanes, ledger as L, market, book as B
from tests.test_pit import add, job

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
[lanes.ci]
usd_per_h = 100
slots = 1
gate = "true"
[pit]
enabled = true
default_stake = 0.25
[autopilot]
max_usd_per_hour = 40
"""
STUB = """#!/bin/sh
{ echo "ARGV: $*"; echo "ROOT: $PIT_ROOT"; cat; } > "$STUB_OUT/$$.txt"
[ -n "$STUB_PROPOSE" ] && mkdir -p "$PIT_ROOT/queue/proposed" && echo 'id = "p1"' > "$PIT_ROOT/queue/proposed/p1.toml"
exit 0
"""


def slow(jid, s=1, **kw):
    return job(jid, run=f"sleep {s}; echo 'pit: job={jid} verdict=pass result={{\"n\": 7}}'", **kw)


class Autopilot(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "lanes.toml").write_text(LANES)
        (self.root / "ledger").mkdir()
        self.stub = self.root / "stubbin"
        self.stub.mkdir()
        (self.stub / "claude").write_text(STUB)
        (self.stub / "claude").chmod(0o755)
        (self.root / "notify.sh").write_text('#!/bin/sh\necho "$1" > "$STUB_OUT/notified"\n')
        (self.root / "notify.sh").chmod(0o755)
        self.out = self.root / "stubout"
        self.out.mkdir()
        env = {"PATH": f"{self.stub}:{os.environ['PATH']}", "STUB_OUT": str(self.out), "PIT_HOST": "t"}
        self.old = {k: os.environ.get(k) for k in (*env, "STUB_PROPOSE")}
        os.environ.update(env)
        self.addCleanup(self.restore)
        self.cfg = lanes.load(self.root)
        self.lg = L.Ledger(self.root / "ledger", "t")
        book = B.Book([])
        for a in ("a", "b"):
            book.agents[a] = self.lg.append(B.agent_row(book, a, f"{a} brief: what {a} is trying to understand"))
        B.tick(self.lg, self.cfg, since="2026-09-29T00:00:00Z")

    def restore(self):
        for k, v in self.old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)

    def post(self, spec, who):
        add(self.lg, {**spec, "proposer": who}, ts=L.now())
        for r in B.stakes(self.lg.rows(), self.cfg, spec, who, "agent"):
            self.lg.append(r)

    def ap(self, **kw):
        ap = A.Autopilot(self.root, self.lg, self.cfg, echo=lambda *_: None, **kw)
        ap.notify = self.root / "notify.sh"
        return ap

    def auto(self, type=None):
        return [r for r in self.lg.rows() if r["t"] == "auto" and (type is None or r["type"] == type)]

    def stub_calls(self):
        return [p.read_text() for p in sorted(self.out.glob("*.txt"))]

    def test_tick_dispatches_every_free_lane_in_parallel_and_hands_back(self):
        self.post(slow("p1"), "a")
        self.post(slow("p2", budget_usd=2), "b")                  # same lane, ranked behind p1: waits
        self.post(slow("c1", lane="ci"), "b")
        for i in (1, 2, 3):
            self.post(slow(f"y{i}", lane="any"), "a")             # two workers on `any`
        self.lg.append(B.bet_row(B.Book(self.lg.rows()), L.fold(self.lg.rows()), "p2", "main", "fail", 0.25, "a"))
        ap = self.ap()
        t0 = time.monotonic()
        ap.tick()
        self.assertEqual({r["job"] for r in self.auto("dispatch")}, {"p2", "c1", "y1", "y2"})   # p2 is matched: it wins gpu-small
        ap.wait()
        self.assertLess(time.monotonic() - t0, 3.5)                # four 1 s runs at once, not one after another
        st = L.fold(self.lg.rows())
        self.assertEqual({j for j, x in st.jobs.items() if x["state"] == "done"}, {"p2", "c1", "y1", "y2"})
        self.assertEqual({r["job"] for r in self.lg.rows() if r["t"] == "claim"}, {"p2", "c1", "y1", "y2"})
        # the next tick hands each result back to its proposer (one sub per wallet) and dispatches what is now free
        ap.tick()
        ap.wait()
        calls = self.stub_calls()
        self.assertEqual(len(calls), 2)
        a_call = next(c for c in calls if "You are a-" in c)
        self.assertIn("ARGV: -p --model sonnet --permission-mode", a_call)
        self.assertIn("--output-format text", a_call)
        self.assertIn(f"ROOT: {self.root}", a_call)
        self.assertIn("a brief: what a is trying to understand", a_call)       # q thread a
        self.assertIn("Your job y1 finished: pass, {\"n\": 7}, cost $", a_call)
        self.assertIn(A.RULES, a_call)
        self.assertIn("You MUST end your turn with exactly one sleep: `q sleep --as a-", a_call)
        book = B.Book(self.lg.rows())
        subs = [r["agent"] for r in self.auto("handback")]
        self.assertTrue(all(book.agents[s]["parent"] in ("a", "b") for s in subs))
        self.assertEqual(sorted(book.wallet(s) for s in subs), ["a", "b"])
        self.assertIn("p1", {r["job"] for r in self.auto("dispatch")})          # gpu-small freed: p1 went next
        self.assertTrue(list((self.root / "autopilot" / "logs").glob("a-*.log")))
        ap.tick()                                                                  # nothing is handed back twice
        ap.wait()
        self.assertEqual(len(self.auto("handback")), 3)                           # + p1's result, to a

    def test_desk_job_wakes_its_proposer_instead_of_running(self):
        self.post(job("d1", lane="ci"), "b")
        ap = self.ap()
        ap.tick()
        ap.wait()
        rows = self.lg.rows()
        self.assertFalse([r for r in rows if r["t"] == "claim"])
        self.assertEqual([(r["agent"], r["reason"]) for r in rows if r["t"] == "wake"], [("b", "desk:d1")])
        self.assertEqual([r["job"] for r in self.auto("wake") if r["reason"].startswith("desk:")], ["d1"])
        (call,) = self.stub_calls()
        self.assertIn("Your desk job d1 is due", call)
        ap.tick()
        self.assertEqual(len([r for r in self.lg.rows() if r["t"] == "wake"]), 1)       # once

    def test_hour_cap_stops_dispatch(self):
        self.post(slow("p1"), "a")
        self.lg.append({"t": "result", "job": "old", "verdict": "pass", "cost": {"usd": 41, "wall_s": 1, "lane": "ci"}})
        ap = self.ap()
        ap.tick()
        ap.tick()
        self.assertEqual(self.auto("dispatch"), [])
        (r,) = self.auto("refuse")                                  # once on the tape, not per tick
        self.assertEqual((r["lane"], r["job"]), ("gpu-small", "p1"))
        self.assertIn("> cap $40.00", r["reason"])
        ap = self.ap(cap=100)                                      # --max-usd-per-hour overrides
        ap.tick()
        ap.wait()
        self.assertEqual([r["job"] for r in self.auto("dispatch")], ["p1"])

    def test_subagent_cap_refuses_then_runs_after_the_window(self):
        self.post(job("d1"), "a")
        self.post(job("d2"), "b")
        ap = self.ap(sub_cap=2)
        ap.auto("start", "test")                               # epoch: what landed since is due
        ap.tick()
        ap.wait()
        self.assertEqual(len(self.auto("handback")), 2)
        self.post(job("d3"), "a")                              # the third quick hand-back
        ap.tick()
        ap.wait()
        self.assertEqual(len(self.auto("handback")), 2)
        self.assertEqual({r["reason"] for r in self.auto("refuse")}, {"subagent cap"})
        self.assertEqual(len(self.stub_calls()), 2)
        m = A.status(self.lg.rows(), {"autopilot": {"max_subagent_runs_per_hour": 2}}, datetime.now(timezone.utc))
        self.assertEqual((m["subagent_runs_hour"], m["subagent_cap"]), (2, 2))
        n = len(self.auto("refuse"))
        ap.tick()                                              # same window: still refused, not repeated on the tape
        ap.wait()
        self.assertEqual((len(self.auto("handback")), len(self.auto("refuse"))), (2, n))
        ap.tick(datetime.now(timezone.utc) + timedelta(hours=2))
        ap.wait()
        self.assertEqual(len(self.auto("handback")), 3)
        self.assertEqual(len(self.stub_calls()), 4)             # + b's refused event wake (d3), retried once the window opened

    # ---- event-driven wakes -------------------------------------------------------------------------
    def wakes(self, prefix="event:"):
        return [(r["agent"], r["reason"]) for r in self.auto("wake") if r["reason"].startswith(prefix)]

    def test_new_post_wakes_both_agents_concurrently_with_a_digest(self):
        ap = self.ap()
        ap.tick()                                              # first tick sets the baseline: nothing to wake
        add(self.lg, {**job("n1", question="is n1 true?"), "proposer": "human"}, ts=L.now())
        ap.tick()
        self.assertEqual(len(ap.subs), 2)                      # both subs alive in the same tick, before any wait
        ap.wait()
        self.assertEqual(sorted(a for a, _ in self.wakes()), ["a", "b"])
        calls = self.stub_calls()
        self.assertEqual(len(calls), 2)
        for c in calls:
            self.assertIn("Since you last looked:\nnew market n1/main PASS $0.00 / FAIL $0.00", c)
            self.assertIn("You MUST end your turn with exactly one sleep", c)
        self.assertEqual({r["note"] for r in self.lg.rows() if r["t"] == "sleep"}, {"auto: sub ended without sleeping"})

    def test_pass_sleeps_until_event_and_is_not_rewoken_without_one(self):
        ap = self.ap()
        ap.tick()
        add(self.lg, {**job("n1"), "proposer": "human"}, ts=L.now())
        ap.tick()
        ap.wait()
        book = B.Book(self.lg.rows())
        for a in ("a", "b"):                                   # stub "passes" with an explicit until-event sleep
            self.lg.append({"t": "sleep", "agent": a, "until": {"event": True}, "note": ""})
        n = len(self.wakes())
        ap.tick()
        ap.tick()
        ap.wait()
        self.assertEqual(len(self.wakes()), n)                 # its own sleep/auto rows are not events
        self.post(job("n2"), "a")                              # a's own post wakes only b
        ap.tick()
        ap.wait()
        self.assertEqual(self.wakes()[n:], [("b", "event:2 rows")])
        self.assertEqual(A.awake(self.lg.rows()), [])        # both subs ended and slept

    def test_events_batch_while_the_sub_runs(self):
        ap = self.ap()
        ap.tick()
        self.post(job("n1"), "a")
        ap.tick()                                              # b wakes (a's own post is not an event for a)
        self.post(job("n2"), "a")
        ap.tick()                                              # b's sub still running or reaped: at most one wake per tick
        ap.wait()
        ap.tick()
        ap.wait()
        self.assertEqual([a for a, _ in self.wakes()], ["b", "b"])

    def test_explicit_conditions_still_win(self):
        ap = self.ap()
        ap.tick()
        self.lg.append({"t": "sleep", "agent": "a", "until": {"balance": 1e9}, "note": "saving"})
        add(self.lg, {**job("n1"), "proposer": "human"}, ts=L.now())
        ap.tick()
        ap.wait()
        self.assertEqual([a for a, _ in self.wakes()], ["b"])   # a waits for its balance, not for the post
        self.lg.append({"t": "sleep", "agent": "a", "until": {"minutes": 1}, "note": "later"})
        ap.tick()
        self.assertEqual(self.wakes("minutes"), [])
        ap.tick(datetime.now(timezone.utc) + timedelta(minutes=2))
        ap.wait()
        self.assertEqual(self.wakes("minutes"), [("a", "minutes")])

    def test_event_wake_cap_refuses_and_retries_oldest_first(self):
        ap = self.ap(sub_cap=1)
        ap.tick()
        add(self.lg, {**job("n1"), "proposer": "human"}, ts=L.now())
        ap.tick()
        ap.wait()
        self.assertEqual(len(self.wakes()), 1)                 # one spawn allowed this hour
        self.assertEqual([r["agent"] for r in self.auto("refuse")], ["b" if self.wakes()[0][0] == "a" else "a"])
        ap.tick()
        self.assertEqual(len(self.wakes()), 1)                 # still capped, not repeated
        ap.tick(datetime.now(timezone.utc) + timedelta(hours=2))
        ap.wait()
        self.assertEqual(sorted(a for a, _ in self.wakes()), ["a", "b"])

    def test_for_ends_the_loop_with_a_stop_row(self):
        self.post(slow("p1"), "a")
        t0 = time.monotonic()
        self.ap(for_="2").loop(interval=0.5)
        self.assertLess(time.monotonic() - t0, 4)
        self.assertEqual([r["reason"] for r in self.auto("stop")], ["for 2 elapsed"])
        self.assertEqual(A.duration("2h30m"), 9000)
        self.assertEqual(A.duration("45m"), 2700)
        self.assertEqual(A.duration("1h"), 3600)
        (start,) = self.auto("start")
        self.assertTrue(start["until"])
        self.assertIsNone(A.status(self.lg.rows(), self.cfg, datetime.now(timezone.utc))["until"])   # stopped: no deadline
        self.ap().wait()

    def test_stop_file_stops_the_loop(self):
        self.post(slow("p1"), "a")
        (self.root / "autopilot").mkdir()
        (self.root / "autopilot" / "STOP").touch()
        self.ap().loop(interval=0)                                 # returns: the STOP file ends it on the first tick
        self.assertEqual(self.auto("dispatch"), [])
        self.assertEqual([r["reason"] for r in self.auto("stop")], ["STOP file"])
        m = market.market_json(self.lg.rows(), self.cfg)["autopilot"]
        self.assertEqual((m["running"], m["stopped"], m["cap"]), (False, True, 40))

    def test_reflect_due_spawns_opus_sub_and_adds_nothing(self):
        self.cfg["reflect"] = {"rows": 1}
        os.environ["STUB_PROPOSE"] = "1"
        jobs = len(L.fold(self.lg.rows()).jobs)
        ap = self.ap()
        ap.tick()
        ap.wait()
        (call,) = self.stub_calls()
        self.assertIn("--model opus", call)
        self.assertIn("reflect digest:", call)
        (r,) = self.auto("reflect")
        book = B.Book(self.lg.rows())
        self.assertEqual((book.agents["reflect"]["kind"], book.agents[r["agent"]]["parent"]), ("persistent", "reflect"))
        self.assertTrue(r["agent"].startswith("reflect-"))
        self.assertEqual(len(L.fold(self.lg.rows()).jobs), jobs)                  # proposals are never added
        self.assertTrue((self.root / "queue" / "proposed" / "p1.toml").exists())
        self.assertIn("p1.toml", (self.out / "notified").read_text())             # the session is woken
        ap.tick()
        self.assertEqual(len(self.auto("reflect")), 1)                            # one pass per reflect cycle

    def test_dry_run_writes_nothing(self):
        self.post(slow("p1"), "a")
        self.post(job("d1"), "b")
        before = self.lg.path.read_text()
        lines = []
        A.Autopilot(self.root, self.lg, self.cfg, dry=True, echo=lines.append).loop(once=True)
        text = "\n".join(lines)
        self.assertEqual(self.lg.path.read_text(), before)
        self.assertFalse((self.root / "autopilot").exists())
        self.assertEqual(self.stub_calls(), [])
        self.assertIn("lane gpu-small (1 slot): gate free · pick p1", text)
        self.assertIn("desk d1: wake b (desk:d1)", text)
        self.assertIn("--- prompt for b-", text)
        self.assertIn("reflection: not due", text)


if __name__ == "__main__":
    unittest.main()
