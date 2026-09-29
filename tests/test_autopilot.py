import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pit import autopilot as A, lanes, ledger as L, market, book as B, reflect
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
model = "small"
runner = "echo RUN {scenario} {model} {budget_s}; echo 'pit: verdict=pass result={{}}'"
preflight = "test -f ok.flag && echo {model}"
[lanes.ci]
usd_per_h = 100
slots = 1
gate = "true"
[pit]
enabled = true
default_stake = 0.25
[autopilot]
idle_wake_gap_s = 1e9      # the re-wake tests set 90; everything else sees no gap re-wakes
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
            self.lg.append({"t": "sleep", "agent": a, "until": {"event": True}, "note": "fixture: last turn ended"})
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

    def test_orphaned_claim_is_settled_invalid(self):
        add(self.lg, job("o", budget_s=60), job("fresh", budget_s=60), job("live", budget_s=60), ts=L.now())
        old = (datetime.now(timezone.utc) - timedelta(seconds=2 * 60 + 61)).strftime("%Y-%m-%dT%H:%M:%SZ")
        new = (datetime.now(timezone.utc) - timedelta(seconds=100)).strftime("%Y-%m-%dT%H:%M:%SZ")
        for j, ts in (("o", old), ("fresh", new), ("live", old)):
            self.lg.append({"t": "claim", "job": j, "lane": "gpu-small", "cid": f"c-{j}"}, ts)
        ap = self.ap()
        ap.runs["live"] = ("gpu-small", None)
        ap.orphans(self.lg.rows(), datetime.now(timezone.utc))
        st = L.fold(self.lg.rows())
        self.assertEqual((st.jobs["o"]["state"], st.jobs["fresh"]["state"], st.jobs["live"]["state"]), ("done", "running", "running"))
        self.assertEqual(st.jobs["o"]["result"]["verdict"], "invalid")
        self.assertEqual(st.jobs["o"]["result"]["note"], "orphaned claim (no live run)")
        self.assertEqual([(r["reason"], r["job"]) for r in self.auto("note")], [("orphan", "o")])

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
        self.assertIn("You MUST end your turn by saying what you are waiting on (`q sleep --as a-", a_call)
        book = B.Book(self.lg.rows())
        subs = [r["agent"] for r in self.auto("handback")]
        self.assertTrue(all(book.agents[s]["parent"] in ("a", "b") for s in subs))
        self.assertEqual(sorted(book.wallet(s) for s in subs), ["a", "b"])
        self.assertIn("p1", {r["job"] for r in self.auto("dispatch")})          # gpu-small freed: p1 went next
        self.assertTrue(list((self.root / "autopilot" / "logs").glob("a-*.log")))
        ap.tick()                                                                  # nothing is handed back twice
        ap.wait()
        self.assertEqual(len(self.auto("handback")), 3)                           # + p1's result, to a

    def test_scenario_spec_dispatches_with_synthesized_run_after_preflight(self):
        self.post(job("sc", scenario="heldout_b", budget_s=300), "a")
        ap = self.ap()
        ap.tick()                                                  # preflight (test -f ok.flag in the root) fails
        ap.wait()
        self.assertFalse([r for r in self.lg.rows() if r["t"] == "claim"])
        self.assertEqual([r["job"] for r in self.auto("refuse")], ["sc"])
        self.assertFalse([r for r in self.lg.rows() if r["t"] == "wake" and r["reason"] == "desk:sc"])   # not desk work
        (self.root / "ok.flag").write_text("")
        ap.tick()
        ap.wait()
        (claim,) = [r for r in self.lg.rows() if r["t"] == "claim"]
        self.assertEqual(claim["run"], "echo RUN heldout_b small 300; echo 'pit: verdict=pass result={}'")
        self.assertEqual(L.fold(self.lg.rows()).jobs["sc"]["result"]["verdict"], "pass")
        self.assertEqual(len(self.auto("refuse")), 1)

    def test_unknown_scenario_is_refused_with_the_list(self):
        from pit import spec as specmod
        s = job("bad", scenario="nope")
        known = ["a_scn", "b_scn"]
        (e,) = specmod.validate(s, self.cfg["lanes"], known)
        self.assertEqual(e, "unknown scenario 'nope' (have: a_scn, b_scn)")
        self.assertEqual(specmod.validate(job("ok", scenario="a_scn"), self.cfg["lanes"], known), [])
        self.assertEqual(specmod.validate(s, self.cfg["lanes"]), [])       # no registry: accept
        self.assertTrue(any("runner" in e for e in specmod.validate(job("c", scenario="a_scn", lane="ci"), self.cfg["lanes"])))

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

    def _rehand(self, ap, n):
        """tick n minutes-past-window later: returns the desk wakes and sub calls so far"""
        ap.tick(datetime.now(timezone.utc) + timedelta(minutes=21 * n))
        ap.wait()
        return [r for r in self.lg.rows() if r["t"] == "wake" and r["reason"] == "desk:d1"]

    def test_unresolved_desk_job_is_rehanded_each_window_then_flagged_stalled(self):
        self.post(job("d1", lane="ci"), "b")
        ap = self.ap()
        ap.auto("start", "test")
        ap.tick()
        ap.wait()
        ap.tick()
        self.assertEqual(len([r for r in self.lg.rows() if r["t"] == "wake" and r["reason"] == "desk:d1"]), 1)   # inside the window
        for n in (1, 2, 3):
            self.assertEqual(len(self._rehand(ap, n)), n + 1)
        self.assertEqual([r["reason"] for r in self.auto("handback") if "desk-retry" in r["reason"]],
                         ["desk-retry:1", "desk-retry:2", "desk-retry:3"])
        self.assertEqual([(r["job"], r["reason"]) for r in self.auto("note")], [("d1", "desk-stalled")])
        desk = [c for c in self.stub_calls() if "Your desk job d1 is due" in c]      # stub files are named by pid: no order
        self.assertEqual(len(desk), 4)
        self.assertTrue(all("Do not leave it queued." in c for c in desk))
        self.assertIn("desk-stalled d1", reflect.digest(self.lg.rows()))
        self._rehand(ap, 4)
        self.assertEqual(len(self.auto("note")), 1)                          # flagged once

    def test_sub_gets_allowed_tools_and_add_dirs(self):
        self.post(job("d1", lane="ci"), "b")
        ap = self.ap()
        ap.c.update(allowed_tools=["Read", "Bash(q *)"], add_dirs=["~/work"])
        ap.tick()
        ap.wait()
        (call,) = self.stub_calls()
        argv = call.splitlines()[0]
        self.assertIn("--allowedTools Read,Bash(q *)", argv)
        self.assertIn(f"--add-dir {Path('~/work').expanduser()}", argv)

    def test_hour_spend_counts_the_last_result_per_job(self):
        now = datetime.now(timezone.utc)
        t = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [{"t": "result", "job": "x", "ts": t, "cost": {"usd": 60}}, {"t": "result", "job": "x", "ts": t, "cost": {"usd": 2}},
                {"t": "result", "job": "y", "ts": t, "cost": {"usd": 1}}]
        self.assertEqual(A.hour_spend(rows, now), 3)            # a correction replaces the row it corrects

    def test_resolved_desk_job_is_not_rehanded(self):
        self.post(job("d1", lane="ci"), "b")
        ap = self.ap()
        ap.auto("start", "test")
        ap.tick()
        ap.wait()
        self.lg.append({"t": "result", "job": "d1", "verdict": "pass", "cost": {"usd": 0, "wall_s": 0, "lane": "ci"}, "agent": "b"})
        self.assertEqual(len(self._rehand(ap, 1)), 1)

    def test_desk_rehand_ignores_the_soft_budget(self):
        self.post(job("d1", lane="ci"), "b")
        ap = self.ap(sub_cap=1)
        ap.auto("start", "test")
        ap.tick()
        ap.wait()
        self.assertEqual(len(self._rehand(ap, 1)), 2)        # over the hour's budget: logged, not blocked
        self.assertEqual(len(self.auto("handback")), 2)

    def test_desk_result_by_proposer_without_run_is_flagged_once(self):
        self.post(job("d1", lane="ci"), "b")
        ap = self.ap()
        ap.tick()
        ap.wait()
        self.lg.append({"t": "result", "job": "d1", "verdict": "pass", "cost": {"usd": 0, "wall_s": 0, "lane": "ci"}, "agent": "b"})
        ap.tick()
        ap.tick()
        self.assertEqual([(r["job"], r["reason"]) for r in self.auto("note")], [("d1", "settled-by-analysis")])

    def test_cli_as_on_result_finding_cancel_decide(self):
        from argparse import Namespace as N
        from pit import cli
        self.post(job("d1", lane="ci"), "b")
        self.post(job("d2", lane="ci"), "b")
        self.lg.append(B.agent_row(B.Book(self.lg.rows()), "b-1", "sub", "b"))
        from unittest import mock
        for name, fake in (("ctx", lambda: (self.root, self.lg, self.cfg)), ("sync", lambda *a: None)):
            p = mock.patch.object(cli, name, fake)
            p.start()
            self.addCleanup(p.stop)
        cli.cmd_result(N(id="d1", verdict="pass", wall_s=0.0, uncached=0, cached=0, out=0, lane=None, arm=None, agent="b-1", force=False))
        cli.cmd_finding(N(id="F:x", source="d1", text="t", kind="finding", refutes=None, refines=None, supersedes=None, agent="b-1"))
        cli.cmd_cancel(N(id="d2", reason="r", agent="b-1"))
        cli.cmd_decide(N(id="F:x", changed=True, note="n", agent="b-1"))
        rows = self.lg.rows()
        got = [r["agent"] for r in rows if r.get("agent") == "b" and (r["t"] in ("result", "cancel", "decision") or r.get("id") == "F:x")]
        self.assertEqual(len(got), 4)
        with self.assertRaises(SystemExit):
            cli.cmd_cancel(N(id="d2", reason="r", agent="nobody"))

    def test_hour_spend_is_reported_not_enforced(self):
        self.post(slow("p1"), "a")
        self.lg.append({"t": "result", "job": "old", "verdict": "pass", "cost": {"usd": 41, "wall_s": 1, "lane": "ci"}})
        ap = self.ap()
        ap.tick()
        ap.wait()
        self.assertEqual([r["job"] for r in self.auto("dispatch")], ["p1"])     # no cap: an open market
        self.assertFalse([r for r in self.auto("refuse") if "hour spend" in r["reason"]])
        m = A.status(self.lg.rows(), self.cfg, datetime.now(timezone.utc))
        self.assertGreaterEqual(m["hour_spend"], 41)
        self.assertEqual(m["cap"], 0)

    def test_subagent_budget_is_soft_and_logged_once_per_hour(self):
        self.post(job("d1"), "a")
        self.post(job("d2"), "b")
        ap = self.ap(sub_cap=1)
        ap.auto("start", "test")
        ap.tick()
        ap.wait()
        self.post(job("d3"), "a")
        ap.tick()
        ap.wait()
        self.assertEqual(len(self.auto("handback")), 3)          # nothing refused by the budget
        budget = [r for r in self.auto("refuse") if r["reason"].startswith("subagent budget")]
        self.assertEqual(len(budget), 1)                          # one row per hour
        m = A.status(self.lg.rows(), {"autopilot": {"max_subagent_runs_per_hour": 2}}, datetime.now(timezone.utc))
        self.assertGreater(m["subagent_runs_hour"], m["subagent_cap"])     # reported

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
            self.assertIn("You MUST end your turn by saying what you are waiting on", c)
        self.assertEqual({r["note"] for r in self.lg.rows() if r["t"] == "sleep" and not r["note"].startswith("fixture")},
                         {"auto: sub ended without sleeping"})

    def test_wake_prompt_carries_the_claim_block(self):
        ap = self.ap()
        ap.tick()
        add(self.lg, {**job("n1"), "proposer": "human"}, ts=L.now())
        ap.tick()
        ap.wait()
        for c in self.stub_calls():
            self.assertIn("Your claim (", c)
            self.assertRegex(c, r"a brief: what a is trying to understand|b brief: what b is trying to understand")
            self.assertIn(A.CLAIM, c)
            self.assertLess(c.index("Your claim ("), c.index("Since you last looked:"))   # claim, experiment, then the board
            self.assertLess(c.index("Your next experiment"), c.index("The board is context"))
            self.assertIn("Your brief is a claim to prove or refute; it is your goal. This turn: (1) state the claim", c)
            self.assertIn("--until-result <job>", c)
            self.assertIn("Every turn must leave the market changed", c)

    def test_heartbeat_wakes_an_idle_funded_agent_once_per_window(self):
        ap = self.ap()
        t0 = datetime.now(timezone.utc)
        ap.tick(t0)
        far = B.Book(self.lg.rows()).balance("b") + 1000       # the fixture drips from 00:00Z: a fixed 999 is crossed by ~17:30Z
        self.lg.append({"t": "sleep", "agent": "b", "until": {"balance": far}, "note": ""})   # b: explicit sleep
        self.lg.append({"t": "node", "kind": "finding", "id": "F1", "text": "x", "agent": "a"})   # a acted just now
        ap.tick(t0 + timedelta(minutes=10))
        ap.wait()
        self.assertEqual(self.wakes(""), [])                     # inside the window: nobody
        self.assertGreaterEqual(B.Book(self.lg.rows()).balance("a"), 1.0)
        ap.tick(t0 + timedelta(minutes=25))
        ap.wait()
        self.assertEqual(self.wakes(""), [("a", "heartbeat")])   # a idle 25 min; b explicitly sleeping stays asleep
        rows = self.lg.rows()                                  # the wake itself starts a new window
        self.assertFalse(ap.idle(rows, B.Book(rows), "a", datetime.now(timezone.utc)))
        c = self.stub_calls()[-1]
        self.assertIn(A.CLAIM, c)

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

    def test_concurrency_cap_refuses_and_retries(self):
        ap = self.ap()
        ap.c["max_concurrent_subagents"] = 1
        ap.tick()
        add(self.lg, {**job("n1"), "proposer": "human"}, ts=L.now())
        ap.tick()
        self.assertEqual(len(self.wakes()), 1)
        self.assertEqual([r["agent"] for r in self.auto("refuse")], ["b" if self.wakes()[0][0] == "a" else "a"])
        ap.wait()
        ap.tick()
        ap.wait()
        self.assertEqual(sorted(a for a, _ in self.wakes()), ["a", "b"])

    # ---- agents never sleep: the gap re-wake, idle lanes ---------------------------------------------
    def rewakes(self):
        return [(r["agent"], r["reason"]) for r in self.lg.rows() if r["t"] == "wake" and r["reason"].startswith("rewake")]

    def test_concurrency_default_is_roots_plus_one(self):
        self.assertEqual(self.ap().max_subs(), 3)                # a, b + reflection
        self.lg.append(B.agent_row(B.Book(self.lg.rows()), "c", "c brief"))
        self.lg.append(B.agent_row(B.Book(self.lg.rows()), "a-1", "sub", "a"))   # subs do not count
        self.assertEqual(self.ap().max_subs(), 4)

    def test_root_with_a_queued_scenario_job_is_rewoken_after_the_gap(self):
        self.post(job("sc", scenario="heldout_b"), "a")   # preflight fails: stays queued
        ap = self.ap()
        ap.c["idle_wake_gap_s"] = 90
        t0 = datetime.now(timezone.utc)
        ap.tick(t0 + timedelta(seconds=30))
        ap.wait()
        self.assertEqual(self.rewakes(), [])                    # inside the gap (fixture turn ended at t0)
        ap.tick(t0 + timedelta(seconds=100))
        ap.wait()
        self.assertEqual(sorted(self.rewakes()), [("a", "rewake: in flight sc"), ("b", "rewake: nothing in flight")])
        c = next(c for c in self.stub_calls() if "You are a-" in c)
        self.assertIn("You were woken: rewake: in flight sc. Agents never sleep", c)
        ap.tick(datetime.now(timezone.utc) + timedelta(seconds=30))   # 30 s after their turns ended: not again yet
        self.assertEqual(len(self.rewakes()), 2)

    def test_own_result_wakes_before_the_gap(self):
        self.post(slow("p1", s=0), "a")
        ap = self.ap()
        ap.c["idle_wake_gap_s"] = 90
        ap.auto("start", "test")
        ap.tick()
        ap.wait()                                               # p1 ran and ended
        ap.tick()
        ap.wait()
        self.assertEqual([r["refs"][0].split(":")[1] for r in self.auto("handback")], ["p1"])
        self.assertEqual(self.rewakes(), [])

    def test_idle_lane_writes_one_idle_row_and_market_json_idle_s(self):
        t = (datetime.now(timezone.utc) - timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.lg.append({"t": "auto", "type": "start", "lane": None, "job": None, "agent": None, "reason": "test"}, t)
        ap = self.ap()
        ap.tick()
        ap.tick()
        ap.wait()
        rows = [r for r in self.auto("idle") if r["lane"] == "gpu-small"]
        self.assertEqual(len(rows), 1)
        self.assertGreaterEqual(rows[0]["seconds"], 360)
        (lane,) = [l for l in market.market_json(self.lg.rows(), self.cfg)["lanes"] if l["lane"] == "gpu-small"]
        self.assertGreaterEqual(lane["idle_s"], 360)
        self.assertIn("Lane gpu-small has been idle 6 min. Idle compute is a bug.", ap.prompt("a-x", "a", []))

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
        self.assertEqual((m["running"], m["stopped"], m["cap"]), (False, True, 0))

    def test_reflect_due_spawns_opus_sub_and_adds_nothing(self):
        self.cfg["reflect"] = {"rows": 1}
        self.lg.append({"t": "node", "kind": "finding", "id": "F0", "text": "one work row to reflect on"})   # agent/drip rows do not count
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
