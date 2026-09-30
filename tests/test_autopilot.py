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
            self.assertIn("Since you last looked:\nnew market n1/main funded $1 (257s) gpu-small · is n1 true?", c)
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
        self.assertIn("New markets since your last turn:\nj2 [ci] does j2 hold?", text)
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


if __name__ == "__main__":
    unittest.main()


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
