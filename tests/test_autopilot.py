import json
import os
import shutil
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pit import autopilot as A, lanes, ledger as L, market, book as B, reflect, spec as specmod
from tests.test_pit import add, job

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
model = "small"
runner = "echo RUN {scenario} {model} {funded_s}; echo 'pit: verdict=pass result={{}}'"
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
echo "turn complete"
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
        self.out = self.root / "stubout"
        self.out.mkdir()
        env = {"PATH": f"{self.stub}:{os.environ['PATH']}", "STUB_OUT": str(self.out), "PIT_HOST": "t"}
        self.old = {k: os.environ.get(k) for k in env}
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
        return ap

    def auto(self, type=None):
        return [r for r in self.lg.rows() if r["t"] == "auto" and (type is None or r["type"] == type)]

    def stub_calls(self):
        return [p.read_text() for p in sorted(self.out.glob("*.txt"))]

    def test_orphaned_claim_is_settled_invalid(self):
        add(self.lg, job("o"), job("fresh"), job("live"), ts=L.now())
        old = (datetime.now(timezone.utc) - timedelta(seconds=specmod.funded_seconds(job("o"), self.cfg["lanes"]) + 61)).strftime("%Y-%m-%dT%H:%M:%SZ")
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

    def test_two_runs_dispatched_in_one_tick_take_distinct_slots(self):
        self.cfg["lanes"]["gpu-small"]["slots"] = 2
        self.post(slow("p1"), "a")
        self.post(slow("p2"), "b")
        ap = self.ap()
        ap.tick()
        self.assertEqual({r["job"] for r in self.auto("dispatch")}, {"p1", "p2"})
        ap.wait()
        slots = {r["job"]: r.get("slot") for r in self.lg.rows() if r["t"] == "claim"}
        self.assertEqual(slots, {"p1": 0, "p2": 1} if slots.get("p1") == 0 else {"p1": 1, "p2": 0})   # never the same slot
        self.assertEqual(ap.slots, {})                                                               # released on reap

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
        self.post(job("sc", scenario="heldout_b"), "a")
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
        self.assertEqual(claim["run"], "echo RUN heldout_b small 257; echo 'pit: verdict=pass result={}'")
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
        cli.cmd_result(N(id="d1", verdict="pass", wall_s=0.0, meter=[], lane=None, arm=None, agent="b-1", force=False))
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
        self.assertEqual(len(self.auto("handback")), 4)          # nothing refused by the budget (d3 also market-wakes b)
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
            self.assertIn("Since you last looked:\nnew market n1/main funded $1 (257s) gpu-small · n1 holds · is n1 true?", c)
            self.assertNotIn("PASS $", c)                          # [pit] blind (default): no pools anywhere in the prompt
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
            self.assertIn("Your brief is a capability or research goal to prove or refute; it is your goal.", c)
            self.assertIn("--until-result <job>", c)
            self.assertIn("Every turn must leave the market changed", c)

    def test_prompt_shows_post_claim_and_claim_record_once(self):
        ap = self.ap()
        ap.seen["a"] = len(self.lg.rows())
        self.post(job("new", claim="The mechanism holds.", question="Does the test pass?"), "b")
        sentence = "The record of every claim and its outcome: `q claims` (grep it as you see fit)."
        for market_wake in (False, True):
            text = ap.prompt("a-turn", "a", [], market=market_wake)
            new = text.split("New markets since your last turn:\n", 1)[1].split(B.NEW_RULE)[0]
            self.assertIn("The mechanism holds. · Does the test pass?", new)
            self.assertEqual(text.count(sentence), 1)
            self.assertNotIn("PASS $", text)
        ap.seen["a"] = len(self.lg.rows())
        text = ap.prompt("a-next", "a", [])
        self.assertNotIn("New markets since your last turn:", text)
        self.assertEqual(text.count(sentence), 1)

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
        self.post(job("n2"), "a")                              # a's own post wakes only b, as a market wake
        ap.tick()
        ap.wait()
        self.assertEqual(self.wakes()[n:], [])
        self.assertIn(("b", "market:n2"), [(r["agent"], r["reason"]) for r in self.auto("wake")])
        self.assertEqual(A.awake(self.lg.rows()), [])        # both subs ended and slept

    def test_events_batch_while_the_sub_runs(self):
        ap = self.ap()
        ap.tick()
        self.post(job("n1"), "a")
        ap.tick()                                              # b wakes for the market (a's own post does not wake a)
        self.post(job("n2"), "a")
        ap.tick()                                              # inside the market floor; sub running or reaped: at most one wake per tick
        ap.wait()
        ap.tick()
        ap.wait()
        self.assertEqual([r["reason"].split(":")[0] for r in self.auto("wake") if r["agent"] == "b"], ["market", "event"])

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

    # ---- a new market is a wake event --------------------------------------------------------------
    def mwakes(self):
        return [(r["agent"], r["reason"]) for r in self.lg.rows() if r["t"] == "wake" and r["reason"].startswith("market:")]

    def test_new_market_wakes_every_other_agent_with_a_floor(self):
        for x in ("c", "d"):
            self.lg.append(B.agent_row(B.Book(self.lg.rows()), x, f"{x} brief"))
            self.lg.append({"t": "sleep", "agent": x, "until": {"event": True}, "note": "fixture: last turn ended"})
        self.lg.append({"t": "retire", "agent": "d", "reason": "done", "by": "reflect"})
        ap = self.ap()
        t0 = datetime.now(timezone.utc)
        ap.tick(t0)                                              # baseline
        self.post(job("m1"), "a")
        ap.tick(t0 + timedelta(seconds=1))
        ap.wait()
        self.assertEqual(sorted(self.mwakes()), [("b", "market:m1"), ("c", "market:m1")])   # not a, not retired d
        self.assertEqual(sorted(r["agent"] for r in self.auto("wake") if r["reason"] == "market:m1"), ["b", "c"])
        c = next(c for c in self.stub_calls() if "You are b-" in c)
        self.assertTrue(c.index("New markets since your last turn:\nm1 [") < c.index(A.MARKET) < c.index("Your claim ("))
        self.assertLess(c.index(A.MARKET), c.index("Standing rules"))
        self.post(job("m2"), "a")
        ap.tick(t0 + timedelta(seconds=10))                      # inside the 30 s floor: nobody
        self.assertEqual(len(self.mwakes()), 2)
        self.post(job("m3"), "b")
        self.post(job("m4"), "b")
        ap.tick(t0 + timedelta(seconds=40))                      # past the floor: two posts coalesce into one wake
        ap.wait()
        self.assertEqual(self.mwakes()[2:], [("a", "market:m3,m4"), ("c", "market:m3,m4")])
        for i in range(5):
            self.post(job(f"n{i}"), "a")
        ap.tick(t0 + timedelta(seconds=80))
        self.assertIn(("b", "market:n0,n1,n2+2"), self.mwakes())

    def test_a_readout_wakes_everyone_with_money_on_the_run_it_informs(self):
        self.lg.append(B.agent_row(B.Book(self.lg.rows()), "c", "c brief"))
        self.lg.append({"t": "sleep", "agent": "c", "until": {"event": True}, "note": "fixture"})
        ap = self.ap()
        t0 = datetime.now(timezone.utc)
        ap.tick(t0)
        self.post(job("x"), "a")
        rows = self.lg.rows()
        self.lg.append(B.bet_row(B.Book(rows), L.fold(rows), "x", "main", "fail", 1, "b", why="no"))
        self.post(job("rd", kind="read", claim="it will ask which client", informs="x", then="t", lane="ci", run="true"), "c")
        ap.tick(t0 + timedelta(seconds=1))
        ap.wait()
        self.lg.append({"t": "result", "job": "rd", "verdict": "read", "result": {"readout": "reads/rd/readout.md"},
                        "cost": {"usd": 0.1, "wall_s": 6, "lane": "ci"}})
        ap.tick(t0 + timedelta(seconds=60))
        ap.wait()
        self.assertEqual(sorted((r["agent"], r["reason"]) for r in self.lg.rows() if r["t"] == "wake" and r["reason"].startswith("read:")),
                         [("a", "read:rd:x"), ("b", "read:rd:x")])                      # proposer and bettor, not the reader
        c = next(c for c in self.stub_calls() if "You are b-" in c and "A read informing x" in c)
        self.assertIn("has landed: reads/rd/readout.md. The reader's claim, written before the readout: it will ask which client", c)
        self.assertIn("before x runs; everyone with money on it has it now",
                      A.finished("rd", L.fold(self.lg.rows()).jobs["rd"]))

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

    def test_lane_whose_device_is_held_by_another_lane_is_not_idle(self):
        t = (datetime.now(timezone.utc) - timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.lg.append({"t": "auto", "type": "start", "lane": None, "job": None, "agent": None, "reason": "test"}, t)
        self.cfg["lanes"]["gpu-small"]["device"] = "gpu0"
        self.cfg["lanes"]["lens"] = {"usd_per_h": 2, "slots": 1, "gate": "true", "device": "gpu0"}
        self.post(slow("r1", lane="lens"), "b")
        self.lg.append({"t": "claim", "job": "r1", "lane": "lens", "cid": "c-r1"})
        ap = self.ap()
        self.assertNotIn("gpu-small", ap.idle_lanes(datetime.now(timezone.utc)))
        self.assertEqual([r for r in self.auto("idle") if r["lane"] == "gpu-small"], [])

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

    def test_reflect_records_on_handback_and_runs_next_cycle(self):
        self.cfg["reflect"] = {"rows": 1}
        self.lg.append({"t": "node", "kind": "finding", "id": "F0", "text": "one work row to reflect on"})
        covered = len(reflect.since_last(self.lg.rows())) + 2     # reflect and its sub are registered before the digest
        ap, now = self.ap(), datetime.now(timezone.utc)
        ap.reflection(now)
        sub = ap.subs[B.REFLECT][0]
        ap.reflection(now)                         # a live pass holds its slot
        self.assertEqual(len(self.auto("reflect")), 1)
        self.lg.append({"t": "node", "kind": "finding", "id": "F1", "text": "arrived during the pass"})
        ap.wait()
        (call,) = self.stub_calls()
        self.assertIn("--model opus", call)
        self.assertIn("reflect digest:", call)
        (record,) = [r for r in self.lg.rows() if r["t"] == "reflect"]
        self.assertEqual((record["agent"], record["rows_covered"]), (sub, covered))
        self.assertEqual(reflect.since_last(self.lg.rows()), [])
        self.assertIsNone(reflect.due(self.lg.rows(), now, self.cfg))
        self.assertEqual(self.auto("note"), [])
        self.assertNotIn(B.REFLECT, ap.subs)
        book = B.Book(self.lg.rows())
        self.assertEqual((book.agents["reflect"]["kind"], book.agents[sub]["parent"]), ("persistent", "reflect"))
        lines = []
        ap.echo = lines.append
        ap.reflection(now)
        self.assertEqual(lines, ["reflection: not due"])
        self.lg.append({"t": "node", "kind": "finding", "id": "F2", "text": "next cycle"})
        ap.reflection(now)
        ap.wait()
        self.assertEqual(len(self.auto("reflect")), 2)
        self.assertEqual(len([r for r in self.lg.rows() if r["t"] == "reflect"]), 2)

    def test_reflect_keeps_the_pass_own_record(self):
        from argparse import Namespace as N
        self.cfg["reflect"] = {"rows": 1}
        self.lg.append({"t": "node", "kind": "finding", "id": "F0", "text": "evidence"})
        ap = self.ap()
        ap.reflection(datetime.now(timezone.utc))
        sub = ap.subs[B.REFLECT][0]
        self.cli_patch().cmd_reflect(N(record=True, note="planted the next direction", agent=sub))
        recorded = [r for r in self.lg.rows() if r["t"] == "reflect"]
        ap.wait()
        ap.reap()
        self.assertEqual([r for r in self.lg.rows() if r["t"] == "reflect"], recorded)
        self.assertEqual(recorded[0]["note"], "planted the next direction")
        self.assertIsNone(reflect.due(self.lg.rows(), datetime.now(timezone.utc), self.cfg))

    def test_dead_reflect_records_and_notes_no_output(self):
        (self.stub / "claude").write_text("#!/bin/sh\nexit 7\n")
        self.cfg["reflect"] = {"rows": 1}
        self.lg.append({"t": "node", "kind": "finding", "id": "F0", "text": "evidence"})
        ap = self.ap()
        ap.reflection(datetime.now(timezone.utc))
        sub = ap.subs[B.REFLECT][0]
        ap.wait()
        (record,) = [r for r in self.lg.rows() if r["t"] == "reflect"]
        self.assertEqual(record["agent"], sub)
        (note,) = self.auto("note")
        self.assertEqual((note["agent"], note["reason"], note["exit_code"]), (sub, "reflection ended without output", 7))
        self.assertIsNone(reflect.due(self.lg.rows(), datetime.now(timezone.utc), self.cfg))

    def test_reflect_failure_with_error_output_still_records(self):
        (self.stub / "claude").write_text("#!/bin/sh\necho failed >&2\nexit 1\n")
        self.cfg["reflect"] = {"rows": 1}
        self.lg.append({"t": "node", "kind": "finding", "id": "F0", "text": "evidence"})
        ap = self.ap()
        ap.reflection(datetime.now(timezone.utc))
        ap.wait()
        self.assertEqual(len([r for r in self.lg.rows() if r["t"] == "reflect"]), 1)
        self.assertEqual(self.auto("note")[0]["reason"], "reflection failed")

    def test_reflect_can_run_after_an_unrecorded_old_spawn(self):
        self.cfg["reflect"] = {"rows": 1}
        self.lg.append({"t": "node", "kind": "finding", "id": "F0", "text": "evidence"})
        ap = self.ap()
        ap.auto("reflect", "rows 1", agent="old-pass")
        ap.reflection(datetime.now(timezone.utc))
        ap.wait()
        self.assertEqual(len(self.auto("reflect")), 2)
        self.assertEqual(len([r for r in self.lg.rows() if r["t"] == "reflect"]), 1)

    def test_runtime_turns_per_hour_caps_spawns_and_the_handback_waits(self):
        from unittest import mock
        self.cfg["autopilot"]["runtimes"] = {"claude": {"model": "sonnet", "turns_per_hour": 1}}
        for jid, who in (("j1", "a"), ("j2", "b")):
            self.post(job(jid), who)
            self.lg.append({"t": "result", "job": jid, "verdict": "fail", "cost": {"usd": 0.1, "lane": "gpu-small"}, "result": {}})
        ap = self.ap()
        said = []
        ap.echo = said.append
        with mock.patch.object(ap, "spawn", return_value=None):
            ap.handback(datetime.now(timezone.utc))
        spawned = [r for r in self.lg.rows() if r["t"] == "agent" and r.get("kind") == "sub"]
        self.assertEqual(len(spawned), 1)                                      # one claude turn this hour
        self.assertTrue(any("waits, runtime claude at its hourly cap (1/1 turns)" in x for x in said))
        self.assertEqual([r for r in self.auto("refuse")], [])                 # a wait, not a refusal

    def test_a_result_on_a_reflect_post_never_wakes_reflect_as_an_agent(self):
        """Reflect's only turns are reflection passes: a result on a root it posted is the next pass's business."""
        from unittest import mock
        book = B.Book(self.lg.rows())
        book.agents[B.REFLECT] = self.lg.append(B.agent_row(book, B.REFLECT, "reflection", None))
        self.lg.append({"t": "node", "kind": "job", "id": "root", "spec": {**job("root"), "proposer": B.REFLECT, "seed": True}})
        self.lg.append({"t": "result", "job": "root", "verdict": "fail", "cost": {"usd": 0.1, "lane": "gpu-small"}, "result": {}})
        ap = self.ap()
        with mock.patch.object(ap, "spawn", side_effect=AssertionError("reflect was spawned for a hand-back")) as sp:
            ap.handback(datetime.now(timezone.utc))
        self.assertEqual([a for a in ap.subs], [])

    def test_reflect_pass_reads_the_operator_notes_verbatim(self):
        self.cfg["reflect"] = {"rows": 1}
        self.cfg["autopilot"]["reflect"] = {**self.cfg["autopilot"].get("reflect", {}), "notes": "agents/GATE.md"}
        (self.root / "agents").mkdir(exist_ok=True)
        (self.root / "agents" / "GATE.md").write_text("# Gate\n- scenario Q fails on dev: counting unit lost\n")
        self.lg.append({"t": "node", "kind": "finding", "id": "F0", "text": "evidence"})
        ap = self.ap()
        texts = []
        ap.spawn = lambda sub, who, text: (texts.append(text), None)[1]
        ap.reflection(datetime.now(timezone.utc))
        self.assertIn("Operator notes (agents/GATE.md), verbatim", texts[0])
        self.assertIn("- scenario Q fails on dev: counting unit lost", texts[0])

    def test_pass_end_endows_planted_agents_from_retired_balances(self):
        self.cfg["reflect"] = {"rows": 1}
        self.lg.append({"t": "node", "kind": "finding", "id": "F0", "text": "evidence"})
        ap = self.ap()
        ap.reflection(datetime.now(timezone.utc))
        sub = ap.subs[B.REFLECT][0]
        b_had = B.Book(self.lg.rows()).balance("b")
        self.lg.append(B.retire_row(B.Book(self.lg.rows()), "b", "goal met", sub))          # what the pass did
        self.lg.append({**B.agent_row(B.Book(self.lg.rows()), "n1", "n1 holds", None), "by": sub})
        ap.wait()
        rows = self.lg.rows()
        endow = [r for r in rows if r["t"] == "endow"]
        self.assertEqual(len(endow), 1)
        self.assertEqual(endow[0]["to"], {"n1": round(b_had, 4)})
        self.assertAlmostEqual(B.Book(rows).balance("n1"), b_had, places=3)

    def test_reflect_prompt_has_agents_facts_and_direct_authority(self):
        self.cfg["reflect"] = {"rows": 1}
        self.post(job("won"), "a")
        self.post(job("lost"), "a")
        for jid, side in (("won", "pass"), ("lost", "pass")):
            self.lg.append(B.bet_row(B.Book(self.lg.rows()), L.fold(self.lg.rows()), jid, "main", side, 1, "b"))
        for jid, verdict in (("won", "pass"), ("lost", "fail")):
            self.lg.append({"t": "result", "job": jid, "verdict": verdict, "cost": lanes.cost_line(self.cfg, "gpu-small", wall_s=30)})
        B.settle_due(self.lg, self.cfg)
        self.lg.append(B.retire_row(B.Book(self.lg.rows()), "b", "goal met: evidence holds", "b"))
        (self.root / "agents").mkdir(exist_ok=True)
        (self.root / "agents" / "BOOTSTRAP.md").write_text("Use the evidence in your next post.")
        before = self.lg.rows()
        ap = self.ap()
        ap.reflection(datetime.now(timezone.utc))
        sub = ap.subs[B.REFLECT][0]
        ap.wait()
        prompt = (self.root / "autopilot" / "logs" / f"{sub}.prompt").read_text()
        facts = json.loads(prompt.split("Agents on the book (", 1)[1].split("): ", 1)[1].split("\n\n", 1)[0])
        self.assertEqual(set(facts), {"a", "b", B.REFLECT})
        self.assertEqual(facts["a"]["record"], {"posts_won": 1, "posts_lost": 1, "bets_won": 0, "bets_lost": 0})
        self.assertEqual(facts["b"]["record"], {"posts_won": 0, "posts_lost": 0, "bets_won": 1, "bets_lost": 1})
        self.assertEqual(facts["a"]["brief"], B.Book(before).agents["a"]["brief"])
        self.assertEqual(facts["b"]["balance"], B.Book(before).balance("b"))
        self.assertFalse(facts["a"]["self_retired"])
        self.assertTrue(facts["b"]["retired"] and facts["b"]["self_retired"])
        self.assertEqual(facts["b"]["retirement_reason"], "goal met: evidence holds")
        self.assertIn(A.RULES, prompt)
        self.assertIn(A.STRUCTURE, prompt)
        self.assertIn("q agent add", prompt)
        self.assertIn("q agent retire", prompt)
        self.assertIn("q post <spec> --as reflect", prompt)
        self.assertIn("Use the evidence in your next post.", prompt)
        self.assertIn(json.dumps(B.newcomer_cost(before)), prompt)
        self.assertIn("does won hold?", prompt)
        self.assertIn("does lost hold?", prompt)
        self.assertNotIn("queue/proposed", prompt)
        self.assertNotIn("the session", prompt)

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

    def cli_patch(self):
        from unittest import mock
        from pit import cli
        for name, fake in (("ctx", lambda: (self.root, self.lg, self.cfg)), ("sync", lambda *a: None)):
            p = mock.patch.object(cli, name, fake)
            p.start()
            self.addCleanup(p.stop)
        return cli

    def test_cross_bet_needs_a_why_which_is_stored_and_on_the_board(self):
        from argparse import Namespace as N
        cli = self.cli_patch()
        self.post(job("j1", lane="ci"), "a")
        with self.assertRaises(SystemExit):
            cli.cmd_bet(N(job="j1", args=["FAIL", "0.10"], agent="b", why=None))
        cli.cmd_bet(N(job="j1", args=["PASS", "0.10"], agent="a", why=None))            # your own post: optional
        cli.cmd_bet(N(job="j1", args=["FAIL", "0.10"], agent="b", why="the small model drops multi-step tasks"))
        bets = [r for r in self.lg.rows() if r["t"] == "bet" and r["job"] == "j1"]
        self.assertEqual(bets[-1]["why"], "the small model drops multi-step tasks")
        self.assertIn('[FAIL b: "the small model drops multi-step tasks"]', B.board(self.lg.rows(), self.cfg))

    def test_wake_prompt_returns_settled_stakes_reflection_and_marks_refuted_findings(self):
        from argparse import Namespace as N
        cli = self.cli_patch()
        F = lambda id, text, agent, refutes=None: cli.cmd_finding(N(id=id, source=None, text=text, kind="finding", refutes=refutes,
                                                                    refines=None, supersedes=None, agent=agent))
        book = B.Book(self.lg.rows())
        self.lg.append(B.agent_row(book, "b-1", "sub", "b"))
        self.lg.append(B.agent_row(B.Book(self.lg.rows()), "reflect", "reflection passes"))
        self.post(job("j1", lane="ci"), "a")
        since = len(self.lg.rows())
        self.post(job("j2", lane="ci"), "a")
        cli.cmd_bet(N(job="j1", args=["FAIL", "0.10"], agent="b-1", why="the small model drops multi-step tasks"))
        F("F:a-1", "ci has no runner for custom jobs", "a")
        F("F:reflect-1", "small-model runs are underfunded: fund 3x", "reflect")
        F("F:a-2", "a ci custom job ran and passed; F:a-1 was wrong", "a", refutes=["F:a-1"])
        F("F:a-3", "per F:a-1, post on the gpu lane instead", "a")
        (self.root / "agents").mkdir()
        (self.root / "agents" / "BOOTSTRAP.md").write_text("- old line\n")
        (self.root / "p.toml").write_text('id = "bs1"\nbootstrap_add = ["- fund small-model runs 3x"]\nbootstrap_remove = ["- old line"]\n')
        cli.cmd_bootstrap(N(apply=str(self.root / "p.toml"), settle=None, n=20, agent="reflect"))
        self.lg.append({"t": "result", "job": "j1", "verdict": "pass", "cost": {"usd": 0.1, "wall_s": 3, "lane": "ci"}})
        B.settle_due(self.lg, self.cfg)
        ap = self.ap()
        ap.seen["b"] = since
        text = ap.prompt("b-2", "b", [])
        self.assertIn('j1 main: you had FAIL $0.10 (you said: "the small model drops multi-step tasks") — lost $0.10', text)
        self.assertIn(B.LOSS_RULE, text)
        self.assertIn("New markets since your last turn:\nj2 [ci] j2 holds · does j2 hold?", text)
        self.assertNotIn(A.DIFF, text)          # no market carries a change
        self.assertNotIn(A.CHANGE, text)        # no workspace configured
        self.assertLess(text.index("Since you last looked:"), text.index("Your stakes that settled since your last turn:"))
        self.assertIn("Reflection since your last turn:\nF:reflect-1: small-model runs are underfunded: fund 3x", text)
        self.assertIn("agents/BOOTSTRAP.md edited (bs1): + - fund small-model runs 3x; - - old line", text)
        self.assertIn("finding F:a-3: per F:a-1 (refuted by a), post on the gpu lane instead", text)
        self.assertIn("finding F:a-1 (refuted by a): ci has no runner", text)
        ap.seen["b"] = len(self.lg.rows())                      # next turn: nothing new, so neither section repeats
        self.assertNotIn("Reflection since your last turn", ap.prompt("b-3", "b", []))
        self.assertNotIn("Your stakes that settled", ap.prompt("b-3", "b", []))

    def device_ap(self):
        """gpu-small and lens share device gpu0; ci has no device."""
        self.cfg["lanes"]["gpu-small"]["device"] = "gpu0"
        self.cfg["lanes"]["lens"] = {"usd_per_h": 2, "slots": 1, "gate": "true", "device": "gpu0"}
        said = []
        ap = self.ap()
        ap.echo = said.append
        return ap, said

    def test_device_running_on_one_lane_keeps_its_other_lanes_out(self):
        ap, said = self.device_ap()
        self.post(slow("g1"), "a")
        self.post(slow("r1", lane="lens"), "b")
        self.lg.append({"t": "claim", "job": "g1", "lane": "gpu-small", "cid": "c-g1"})    # g1 runs on the ledger, not a child
        ap.dispatch(self.lg.rows(), datetime.now(timezone.utc))
        ap.wait()
        self.assertEqual(self.auto("dispatch"), [])
        self.assertIn("lane lens: device gpu0 busy (g1 running on gpu-small)", said)
        self.assertEqual(market.market_json(self.lg.rows(), self.cfg)["lanes"][-1]["device"], "gpu0")

    def test_device_both_free_dispatches_exactly_one_per_tick(self):
        ap, said = self.device_ap()
        self.post(slow("g1"), "a")
        self.post(slow("r1", lane="lens"), "b")
        self.post(slow("c1", lane="ci"), "b")                                          # no device: unaffected
        ap.dispatch(self.lg.rows(), datetime.now(timezone.utc))
        self.assertEqual({r["job"] for r in self.auto("dispatch")}, {"g1", "c1"})
        self.assertIn("lane lens: device gpu0 busy (g1 running on gpu-small)", said)
        ap.wait()

    def test_device_lanes_take_turns(self):
        """gpu-small claimed last, so with both free the lens lane goes first this tick."""
        ap, said = self.device_ap()
        self.post(slow("g0"), "a")
        self.lg.append({"t": "claim", "job": "g0", "lane": "gpu-small", "cid": "c-g0"})
        self.lg.append({"t": "result", "job": "g0", "verdict": "pass", "cost": {"usd": 0.1}, "result": {}})
        self.post(slow("g1"), "a")
        self.post(slow("r1", lane="lens"), "b")
        ap.dispatch(self.lg.rows(), datetime.now(timezone.utc))
        self.assertEqual([r["job"] for r in self.auto("dispatch")], ["r1"])
        self.assertIn("lane gpu-small: device gpu0 busy (r1 running on lens)", said)
        ap.wait()

    def test_lanes_without_a_device_are_unaffected(self):
        self.cfg["lanes"]["lens"] = {"usd_per_h": 2, "slots": 1, "gate": "true", "device": "gpu0"}
        ap = self.ap()
        self.post(slow("g1"), "a")
        self.post(slow("r1", lane="lens"), "b")
        self.post(slow("c1", lane="ci"), "b")
        ap.dispatch(self.lg.rows(), datetime.now(timezone.utc))
        self.assertEqual({r["job"] for r in self.auto("dispatch")}, {"g1", "r1", "c1"})
        ap.wait()


class Retire(unittest.TestCase):
    setUp, restore, ap, auto = Autopilot.setUp, Autopilot.restore, Autopilot.ap, Autopilot.auto

    def test_max_agents_caps_planting_until_a_retirement(self):
        from argparse import Namespace as N
        from unittest import mock
        from pit import cli
        cfg = {**self.cfg, "pit": {**self.cfg["pit"], "max_agents": 2}}     # a and b are active
        add = lambda i: cli.cmd_agent(N(verb="add", id=i, brief=f"{i} holds", parent=None, runtime=None, model=None, reason=None, by="reflect"))
        with mock.patch.object(cli, "ctx", lambda: (self.root, self.lg, cfg)), mock.patch.object(cli, "sync", lambda *a: None):
            with self.assertRaises(SystemExit) as e:
                add("c")
            self.assertEqual(str(e.exception), "2 agents active (max 2): retire one first (q agent retire <id> --reason '…')")
            cli.cmd_agent(N(verb="retire", id="b", brief=None, parent=None, reason="goal met", by="b"))
            add("c")
        self.assertEqual(B.Book(self.lg.rows()).active(), ["a", "c"])

    def test_seats_fix_the_population_composition(self):
        from argparse import Namespace as N
        from unittest import mock
        from pit import cli
        cfg = {**self.cfg, "pit": {**self.cfg["pit"], "seats": {"claude/sonnet": 2, "claude/opus": 1}},
               "autopilot": {**self.cfg["autopilot"], "runtimes": {"claude": {"model": "sonnet"}}}}
        add = lambda i, rt=None, m=None: cli.cmd_agent(N(verb="add", id=i, brief=f"{i} holds", parent=None, runtime=rt, model=m, reason=None, by="reflect"))
        with mock.patch.object(cli, "ctx", lambda: (self.root, self.lg, cfg)), mock.patch.object(cli, "sync", lambda *a: None):
            add("o1")                                                   # a and b fill claude/sonnet: the free seat is opus
            self.assertEqual(B.Book(self.lg.rows()).agents["o1"]["model"], "opus")
            with self.assertRaises(SystemExit) as e:
                add("x")
            self.assertIn("every seat is full", str(e.exception))
            cli.cmd_agent(N(verb="retire", id="o1", brief=None, parent=None, reason="goal met", by="o1"))
            with self.assertRaises(SystemExit) as e:
                add("s3", "claude", "sonnet")                           # the sonnet seats are still full
            self.assertIn("seat claude/sonnet is full", str(e.exception))
            add("o2")                                                   # the vacated opus seat is what a new agent gets
            self.assertEqual(B.Book(self.lg.rows()).agents["o2"]["model"], "opus")

    def test_retired_agent_gets_no_wake_and_no_drip(self):
        from argparse import Namespace as N
        from unittest import mock
        from pit import cli
        with mock.patch.object(cli, "ctx", lambda: (self.root, self.lg, self.cfg)), mock.patch.object(cli, "sync", lambda *a: None):
            cli.cmd_agent(N(verb="retire", id="b", brief=None, parent=None, reason="capability proven", by="reflect"))
            with self.assertRaises(SystemExit):
                cli.cmd_agent(N(verb="retire", id="b", brief=None, parent=None, reason="again", by=None))
        book = B.Book(self.lg.rows())
        self.assertEqual((book.retired["b"]["reason"], book.active()), ("capability proven", ["a"]))
        ap = self.ap()
        ap.c = {**ap.c, "idle_wake_gap_s": 0, "heartbeat_minutes": 0}
        ap.tick()
        ap.wait()
        woken = {r["agent"] for r in self.lg.rows() if r["t"] == "wake"} | {r.get("agent") for r in self.auto("wake")}
        subs_of = {r["parent"] for r in self.lg.rows() if r["t"] == "agent" and r.get("parent")}
        self.assertNotIn("b", woken | subs_of)
        self.assertIn("a", subs_of)                                                  # the live agent still gets its turn
        later = (datetime.now(timezone.utc) + timedelta(minutes=10))
        drip = B.tick(self.lg, self.cfg, later)
        self.assertEqual(list(drip["to"]), ["a"])
        m = {x["id"]: x for x in market.market_json(self.lg.rows(), self.cfg)["agents"]}
        self.assertEqual((m["b"]["retired"]["reason"], m["a"]["retired"]), ("capability proven", None))   # on the book, dimmed
        self.assertAlmostEqual(B.Book(self.lg.rows()).balance("b"), book.balance("b"))

    def test_self_retired_agent_gets_no_wake_and_no_drip(self):
        from argparse import Namespace as N
        from unittest import mock
        from pit import cli
        with mock.patch.object(cli, "ctx", lambda: (self.root, self.lg, self.cfg)), mock.patch.object(cli, "sync", lambda *a: None):
            cli.main(["agent", "retire", "b", "--reason", "goal met: capability proven", "--as", "b"])
            with self.assertRaises(SystemExit):
                cli.cmd_agent(N(verb="retire", id="b", brief=None, parent=None, reason="again", by=None))
        book = B.Book(self.lg.rows())
        self.assertEqual((book.retired["b"]["reason"], book.active()), ("goal met: capability proven", ["a"]))
        self.assertEqual(book.retired["b"]["by"], "b")
        ap = self.ap()
        ap.c = {**ap.c, "idle_wake_gap_s": 0, "heartbeat_minutes": 0}
        ap.tick()
        ap.wait()
        woken = {r["agent"] for r in self.lg.rows() if r["t"] == "wake"} | {r.get("agent") for r in self.auto("wake")}
        subs_of = {r["parent"] for r in self.lg.rows() if r["t"] == "agent" and r.get("parent")}
        self.assertNotIn("b", woken | subs_of)
        self.assertIn("a", subs_of)                                                  # the live agent still gets its turn
        later = (datetime.now(timezone.utc) + timedelta(minutes=10))
        drip = B.tick(self.lg, self.cfg, later)
        self.assertEqual(list(drip["to"]), ["a"])
        m = {x["id"]: x for x in market.market_json(self.lg.rows(), self.cfg)["agents"]}
        self.assertEqual((m["b"]["retired"]["reason"], m["a"]["retired"]), ("goal met: capability proven", None))   # on the book, dimmed
        self.assertAlmostEqual(B.Book(self.lg.rows()).balance("b"), book.balance("b"))


class Bootstrap(unittest.TestCase):
    setUp, restore, post, ap = Autopilot.setUp, Autopilot.restore, Autopilot.post, Autopilot.ap

    def done(self, jid, verdict):
        self.lg.append({"t": "result", "job": jid, "verdict": verdict, "cost": {"usd": 0, "wall_s": 0, "lane": "ci"}})

    def cli(self, **kw):
        from argparse import Namespace as N
        from unittest import mock
        from pit import cli
        with mock.patch.object(cli, "ctx", lambda: (self.root, self.lg, self.cfg)), mock.patch.object(cli, "sync", lambda *a: None):
            cli.cmd_bootstrap(N(**{"apply": None, "settle": None, "n": 2, "agent": "reflect", **kw}))

    def test_bootstrap_text_is_in_the_wake_prompt(self):
        (self.root / "agents").mkdir()
        (self.root / "agents" / "BOOTSTRAP.md").write_text("- check q board before posting\n")
        text = self.ap().prompt("a-x", "a", [])
        self.assertIn("- check q board before posting", text)
        self.assertLess(text.index(A.RULES), text.index("- check q board before posting"))

    def test_bootstrap_cost_counts_invalids_among_the_first_n(self):
        for j in ("j1", "j2", "j3"):
            self.post(job(j, lane="ci"), "a")
        self.done("j1", "invalid")
        self.lg.append({"t": "cancel", "id": "j2", "reason": "dup"})
        self.done("j3", "invalid")
        c = B.bootstrap_cost(self.lg.rows(), "a", n=2)
        self.assertEqual((c["posts"], c["invalid"], c["cancelled"]), (2, 0.5, 0.5))
        self.assertEqual(B.newcomer_cost(self.lg.rows())["agent"], "b")
        self.assertEqual(market.market_json(self.lg.rows(), self.cfg)["top"]["newcomer_cost"]["agent"], "b")

    def test_apply_edits_the_file(self):
        (self.root / "agents").mkdir()
        (self.root / "agents" / "BOOTSTRAP.md").write_text("# how\n- old line\n- keep\n")
        spec = self.root / "p.toml"
        spec.write_text('id = "bs1"\nbootstrap_add = ["- new line"]\nbootstrap_remove = ["- old line"]\n')
        self.cli(apply=str(spec))
        self.assertEqual((self.root / "agents" / "BOOTSTRAP.md").read_text(), "# how\n- keep\n- new line\n")

    def test_settle_records_pass_when_the_cost_drops(self):
        self.lg.append(B.agent_row(B.Book(self.lg.rows()), "reflect", "reflection"))
        for j in ("b1", "b2"):
            self.post(job(j, lane="ci"), "b")
            self.done(j, "invalid")
        add(self.lg, job("bs1", lane="any"), ts=L.now())
        self.lg.append(B.agent_row(B.Book(self.lg.rows()), "c", "newcomer after the edit"))
        add(self.lg, job("c1", lane="ci", proposer="c"), ts=L.now())
        with self.assertRaises(SystemExit):           # 1/2 posts in: too early to settle
            self.cli(settle="bs1")
        add(self.lg, job("c2", lane="ci", proposer="c"), ts=L.now())
        self.done("c1", "pass")
        self.done("c2", "invalid")
        self.cli(settle="bs1")
        r = L.fold(self.lg.rows()).jobs["bs1"]["result"]
        self.assertEqual((r["verdict"], r["agent"], r["result"]["before"]["agent"], r["result"]["after"]["agent"]), ("pass", "reflect", "b", "c"))


class FreshLedgerIncome(unittest.TestCase):
    def test_first_tick_pays_from_the_start_row(self):
        import tempfile
        from pathlib import Path
        from datetime import datetime, timedelta, timezone
        from pit import ledger as L, book as B, autopilot as A, lanes
        root = Path(tempfile.mkdtemp()); (root / "ledger").mkdir()
        lg = L.Ledger(root / "ledger")
        lg.append({"t": "agent", "id": "a", "kind": "persistent", "brief": "x"})
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        lg.append({"t": "auto", "type": "start", "reason": "test"}, B.iso(t0))
        cfg = lanes.load(Path(__file__).resolve().parent.parent / "examples" / "replay-synthetic") if hasattr(lanes, "load") else None
        rows_before = len(lg.rows())
        drip = B.tick(lg, cfg, t0 + timedelta(minutes=3), since=B.iso(t0))
        self.assertIsNotNone(drip)
        self.assertEqual(drip["minutes"], 3)
        self.assertGreater(drip["to"]["a"], 0)



class Runtimes(unittest.TestCase):
    """Agents are model-agnostic: spawn() builds the argv of the agent row's runtime."""
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "lanes.toml").write_text(LANES + '''add_dirs = ["/x/repo"]
runtimes.claude.model = "big"
runtimes.codex.model = "gpt-x"
''')
        self.cfg = lanes.load(self.root)
        self.ap = A.Autopilot(self.root, L.Ledger(self.root / "ledger", "t"), self.cfg, echo=lambda *_: None)

    def argv(self, row):
        from unittest import mock
        with mock.patch.object(A.shutil, "which", lambda n: f"/bin/{n}"), mock.patch.object(A.subprocess, "Popen") as P:
            self.ap.spawn("s-1", row, "the prompt")
        (cmd,), kw = P.call_args
        self.assertEqual(kw["cwd"], self.root)
        self.assertEqual(kw["env"]["PIT_ROOT"], str(self.root))
        self.assertTrue(kw["env"]["PATH"].startswith(str(A.REPO / "bin")))
        self.assertEqual((self.root / "autopilot" / "logs" / "s-1.prompt").read_text(), "the prompt")
        return cmd

    def test_claude_default(self):
        cmd = self.argv({"id": "a", "brief": "b"})
        self.assertEqual(cmd[:4], ["/bin/claude", "-p", "--model", "big"])
        self.assertIn("--permission-mode", cmd)
        self.assertEqual(cmd[-2:], ["--add-dir", "/x/repo"])

    def test_codex_row(self):
        cmd = self.argv({"id": "a", "brief": "b", "runtime": "codex", "model": "gpt-y"})
        self.assertEqual(cmd, ["/bin/codex", "exec", "-m", "gpt-y", *A.CODEX_ARGS, "--add-dir", "/x/repo", "-C", str(self.root), "-"])
        self.assertEqual(self.argv({"id": "a", "brief": "b", "runtime": "codex"})[2:4], ["-m", "gpt-x"])   # runtimes.codex.model
        (self.root / ".git").mkdir()
        self.assertEqual(self.argv({"id": "a", "runtime": "codex"})[-5:-3], ["--add-dir", str(self.root / ".git")])   # q commits

    def test_reflection_runtime_and_model(self):
        from unittest import mock
        self.ap.cfg["reflect"] = {"rows": 1}
        self.ap.lg.append({"t": "node", "kind": "finding", "id": "F0", "text": "evidence"})
        for settings, runtime, model in (({}, "claude", "opus"),
                                         ({"runtime": "codex"}, "codex", "gpt-x"),
                                         ({"runtime": "codex", "model": "gpt-y"}, "codex", "gpt-y")):
            with self.subTest(settings=settings):
                self.ap.c["reflect"] = settings
                self.ap.subs.clear()
                with mock.patch.object(A.shutil, "which", lambda n: f"/bin/{n}"), mock.patch.object(A.subprocess, "Popen") as popen:
                    self.ap.reflection(datetime.now(timezone.utc))
                (cmd,), _ = popen.call_args
                self.assertEqual(cmd[:4], [f"/bin/{runtime}", "exec" if runtime == "codex" else "-p",
                                          "-m" if runtime == "codex" else "--model", model])

    def test_agent_add_set_and_unknown_runtime(self):
        book = B.Book([])
        with self.assertRaises(SystemExit):
            B.agent_row(book, "a", "b", runtime="gpt")
        lg = L.MemLedger()
        lg.append(B.agent_row(book, "a", "brief a"))
        lg.append(B.agent_set_row(B.Book(lg.rows()), "a", "codex", "gpt-y"), "2099-01-01T00:00:00Z")
        r = B.Book(lg.rows()).agents["a"]
        self.assertEqual((r["runtime"], r["model"], r["brief"], r["kind"]), ("codex", "gpt-y", "brief a", "persistent"))
        self.assertNotIn("model", B.agent_set_row(B.Book(lg.rows()), "a", "claude"))      # a runtime switch drops the model
        with self.assertRaises(SystemExit):
            B.agent_set_row(B.Book(lg.rows()), "a", "gpt")


class Behind(unittest.TestCase):
    def test_commits_behind_the_lanes_base(self):
        import tempfile, subprocess
        repo = Path(tempfile.mkdtemp())
        def g(*a): return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout
        g("init", "-q", "-b", "main"); g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "one")
        g("branch", "agent"); g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "two")
        g("checkout", "-q", "agent")
        cfg = {"lanes": {"ci": {"repo": str(repo), "base": "main"}}}
        self.assertEqual(A.behind(repo, cfg), (1, "main"))
        self.assertIsNone(A.behind(repo, {"lanes": {"ci": {}}}))       # no repo lane base: no fact
        self.assertIsNone(A.behind(repo / "missing", cfg))


class Workspaces(unittest.TestCase):
    def setUp(self):
        from pit import trees
        self.trees = trees
        self.tmp = tempfile.TemporaryDirectory(prefix='pit-workspaces-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        trees.git(self.repo, 'init', '-q', '-b', 'main')
        trees.git(self.repo, 'config', 'user.name', 'Test')
        trees.git(self.repo, 'config', 'user.email', 'test@example.com')
        trees.git(self.repo, 'commit', '--allow-empty', '-qm', 'base')
        self.lg = L.Ledger(self.root / 'ledger', 't')
        self.lg.append(B.agent_row(B.Book([]), 'a', 'capability'))
        self.lg.append(B.agent_row(B.Book(self.lg.rows()), 'a-turn', 'turn', 'a'))
        self.cfg = {'lanes': {}, 'autopilot': {
            'workspace': str(self.root / 'agent trees/{agent}'),
            'workspace_init': f'git -C {self.repo} worktree add -b test/{{agent}} {{path}} main'}}
        self.ap = A.Autopilot(self.root, self.lg, self.cfg, echo=lambda *_: None)

    def test_workspace_once_per_wallet_both_runtimes_and_linked_git_dirs(self):
        from unittest import mock
        tree = self.ap.workspace('a')
        (tree / 'keep.txt').write_text('edits survive turns')
        self.assertEqual(self.ap.workspace('a'), tree)
        self.assertTrue((tree / 'keep.txt').exists())
        real_popen = A.subprocess.Popen
        for rt in ('claude', 'codex'):
            spawned = []
            def popen(cmd, **kw):
                if cmd[0] == f'/bin/{rt}':
                    spawned.append(cmd)
                    return mock.Mock()
                return real_popen(cmd, **kw)
            with mock.patch.object(A.shutil, 'which', return_value=f'/bin/{rt}'), mock.patch.object(A.subprocess, 'Popen', side_effect=popen):
                self.ap.spawn('a-turn', {'runtime': rt}, A.CHANGE)
            cmd = spawned[0]
            self.assertIn(str(tree), cmd)
            self.assertEqual(cmd[cmd.index(str(tree)) - 1], '--add-dir')
            text = (self.root / 'autopilot/logs/a-turn.prompt').read_text()
            self.assertIn(f'Your working tree: {tree}, branch test/a; commit there, then post with --ref', text)
            self.assertIn('PR candidate for a human', text)
            if rt == 'codex':
                for opt in ('--git-dir', '--git-common-dir'):
                    self.assertIn(self.trees.git(tree, 'rev-parse', '--path-format=absolute', opt).strip(), cmd)
        self.assertFalse((self.root / 'agent trees/a-turn').exists())

    def test_change_market_tells_bettor_how_to_inspect_and_reflect_has_no_tree(self):
        self.lg.append(B.agent_row(B.Book(self.lg.rows()), 'b', 'other'))
        s = job('c1', proposer='b', ref='1' * 40, ref_name='feature', base_ref='2' * 40)
        add(self.lg, s)
        self.ap.seen['a'] = 0
        text = self.ap.prompt('a-turn', 'a', [])
        self.assertIn('New markets since your last turn:\nc1 ◇ [gpu-small]', text)
        self.assertIn(A.DIFF, text)
        self.assertIn(A.CHANGE, text)
        self.assertIsNone(self.ap.workspace(B.REFLECT))

    def test_init_failure_prevents_spawn_and_dry_run_creates_nothing(self):
        self.ap.c['workspace_init'] = 'exit 4'
        with self.assertRaisesRegex(SystemExit, 'workspace_init for a failed'):
            self.ap.workspace('a')
        dry = A.Autopilot(self.root, self.lg, self.cfg, dry=True, echo=lambda *_: None)
        dry.spawn('a-turn', {'runtime': 'claude'}, 'prompt')
        self.assertFalse((self.root / 'agent trees').exists())
        self.assertFalse((self.root / 'autopilot').exists())


if __name__ == "__main__":
    unittest.main()
