import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import sys
import tomllib
import unittest
from unittest import mock
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pit.metrics import metrics, table
from pit import hooks, market, view, reflect, lanes, ledger as L, book as B, replay, run as runmod, spec as specmod

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "replay-synthetic"
CFG = tomllib.loads((ROOT / "lanes.example.toml").read_text())


def job(id, **kw):
    s = {"id": id, "question": f"does {id} hold?", "if_pass": "go", "if_fail": "stop",
         "lane": "gpu-small", "budget_usd": 1}
    s.update(kw)
    return specmod.normalize(s)


def add(lg, *specs, ts="2026-09-28T00:00:00Z"):
    for s in specs:
        lg.append({"t": "node", "kind": "job", "id": s["id"], "spec": s}, ts)


class Validator(unittest.TestCase):
    def errs(self, **kw):
        return specmod.validate(job("a", **kw), CFG["lanes"])

    def test_funding_buys_between_30_s_and_an_hour(self):
        self.assertEqual(self.errs(budget_usd=14, value=1), [])                               # the hour on a $14/h lane
        self.assertTrue(any("no run longer than an hour" in e for e in self.errs(budget_usd=14.1, value=1)))
        self.assertTrue(any("no budget_s" in e for e in self.errs(budget_s=60)))                # money, not time

    def test_ok(self):
        self.assertEqual(self.errs(), [])

    def test_refusals(self):
        self.assertIn("missing question", self.errs(question=""))
        for e in ("pass", "fail"):                                                # a post claims it will pass
            self.assertIn("no expect: a post claims the run will pass; to say something fails, bet FAIL on another "
                          "agent's post", self.errs(expect=e))
        self.assertTrue(any("if_pass == if_fail" in e for e in self.errs(if_fail="go")))
        self.assertTrue(any("unknown lane" in e for e in self.errs(lane="gpu9")))
        self.assertTrue(any("cheap lane" in e for e in self.errs(budget_usd=30)))      # gpu-small: $25/value
        self.assertTrue(any("no run longer than an hour" in e for e in self.errs(budget_usd=30, value=2)))   # $30 on a $14/h lane buys > 3600 s
        self.assertEqual(self.errs(budget_usd=10, value=2), [])                                  # $10 buys 2571 s on gpu-small
        self.assertTrue(any("no run longer than an hour" in e for e in specmod.validate(job("a", lane="gpu-large", budget_usd=500), CFG["lanes"])))
        self.assertTrue(any("funding buys < 30 s on gpu-small" in e for e in self.errs(budget_usd=0.1)))   # $0.10 at $14/h = 25 s
        self.assertTrue(any("not in depends_on" in e for e in self.errs(inputs={"x": "{{ jobs.b.result.k }}"})))
        self.assertTrue(any("unresolved" in e for e in self.errs(inputs={"x": "{{ nonsense }}"})))
        self.assertEqual(self.errs(depends_on=["b"], inputs={"x": "{{ jobs.b.result.k }}"}), [])


class Templates(unittest.TestCase):
    def test_fill(self):
        s = job("a", depends_on=["b", "F:x"], inputs={"arms": "{{ jobs.b.result.top3 }}", "why": "{{ findings.F:x.text }}"},
                run="echo {{ inputs.arms }} {{ jobs.b.verdict }}")
        ctx = {"jobs": {"b": {"verdict": "pass", "result": {"top3": ["V1", "it's"]}}}, "findings": {"F:x": {"text": "cap"}}}
        inputs, run = specmod.render(s, ctx)
        self.assertEqual(inputs, {"arms": ["V1", "it's"], "why": "cap"})   # whole template keeps the list
        self.assertEqual(run, "echo V1 'it'\"'\"'s' pass")                  # quoted into the shell

    def test_unresolved_at_render(self):
        s = job("a", depends_on=["b"], inputs={"x": "{{ jobs.b.result.missing }}"})
        with self.assertRaises(specmod.SpecError):
            specmod.render(s, {"jobs": {"b": {"verdict": "pass", "result": {}}}, "findings": {}})


class Fold(unittest.TestCase):
    def test_frontier_and_refutes_stale(self):
        lg = L.MemLedger()
        lg.append({"t": "node", "kind": "hypothesis", "id": "H:cap", "text": "cause = cap"}, "2026-09-28T22:00:00Z")
        add(lg, job("rung", produces=["F:cap"]), job("variants", depends_on=["F:cap"]),
            job("after", depends_on=["variants"]), job("refuter", depends_on=["F:cap"], refutes_if_pass=["F:cap"],
                                                     produces=["F:page"]), job("uses-page", depends_on=["F:page"]))
        st = L.fold(lg.rows())
        self.assertEqual(st.frontier(), ["rung"])
        self.assertEqual(st.why_blocked("variants"), ["waiting on F:cap (not in the ledger yet)"])
        L.settle(lg, st.jobs["rung"]["spec"], "pass", "2026-09-28T22:27:00Z")
        lg.append({"t": "result", "job": "rung", "verdict": "pass", "cost": {"usd": 1, "wall_s": 1, "lane": "x"}},
                  "2026-09-28T22:27:00Z")
        self.assertEqual(sorted(L.fold(lg.rows()).frontier()), ["refuter", "variants"])
        lg.append({"t": "result", "job": "refuter", "verdict": "pass", "cost": {"usd": 1, "wall_s": 1, "lane": "x"}},
                  "2026-09-28T22:47:00Z")
        L.settle(lg, L.fold(lg.rows()).jobs["refuter"]["spec"], "pass", "2026-09-28T22:47:00Z")
        st = L.fold(lg.rows())
        self.assertEqual(st.findings["F:cap"]["status"], "refuted")
        self.assertIn("variants", st.stale)
        self.assertIn("after", st.stale)             # transitively downstream
        self.assertNotIn("refuter", st.stale)        # the evidence is not a casualty
        self.assertNotIn("uses-page", st.stale)      # rests only on the refuter's own finding
        self.assertEqual(st.stale["variants"]["ts"], "2026-09-28T22:47:00Z")
        self.assertEqual(st.frontier(), ["uses-page"])
        lg.append({"t": "review", "id": "variants", "note": "arms still answer the ordering question"})
        self.assertNotIn("variants", L.fold(lg.rows()).stale)

    def test_branch_deps_and_cancel(self):
        lg = L.MemLedger()
        add(lg, job("review"), job("cut", depends_on=["review@pass"]), job("x"))
        lg.append({"t": "result", "job": "review", "verdict": "fail", "cost": {"usd": 0, "wall_s": 1, "lane": "any"}})
        lg.append({"t": "cancel", "id": "x", "reason": "dead end: refuted upstream"})
        st = L.fold(lg.rows())
        self.assertIn("dead branch", st.why_blocked("cut")[0])
        self.assertEqual(st.jobs["x"]["state"], "cancelled")
        self.assertEqual(st.frontier(), [])


class Cost(unittest.TestCase):
    def test_cost_line(self):
        c = lanes.cost_line(CFG, "gpu-large", 36, {"tok_in": 1_000_000, "tok_cached": 1_000_000, "tok_out": 1_000_000})
        self.assertAlmostEqual(c["usd"], 0.24 + 0.05 + 2.20 + 1.0)
        self.assertEqual((c["usd_time"], c["wall_s"], "unpriced" in c), (1.0, 36.0, False))
        self.assertAlmostEqual(lanes.cost_line(CFG, "gpu-small", wall_s=3600)["usd"], 14)
        self.assertAlmostEqual(lanes.cost_line(CFG, "ci", wall_s=17 * 60)["usd"], 28.3333, places=3)

    def test_meters_are_priced_by_the_lane(self):
        cfg = {**CFG, "lanes": {**CFG["lanes"], "ci": {**CFG["lanes"]["ci"], "prices": {"usd_per_mtok_out": 10, "usd_per_gb": 0.5}}}}
        c = lanes.cost_line(cfg, "ci", 0, {"tok_in": 2_000_000, "tok_out": 100_000, "gb": 3, "widgets": 7})
        self.assertAlmostEqual(c["usd"], 2 * 0.24 + 0.1 * 10 + 3 * 0.5)          # [prices] default, the lane's own over it
        self.assertEqual((c["meters"]["widgets"], c["unpriced"]), (7, ["widgets"]))   # recorded, not charged
        self.assertAlmostEqual(lanes.cost_line(CFG, "any", 3600, {"tok_out": 1_000_000})["usd"], 2.20)   # `any`: meters only

    def test_parse_line(self):
        r = runmod.parse_line('pit: job=a verdict=fail wall_s=4.5 meters={"tok_in": 10, "tok_cached": 20, "tok_out": 3} result={"k": [1, 2]}')
        self.assertEqual(r, {"job": "a", "verdict": "fail", "wall_s": 4.5, "meters": {"tok_in": 10, "tok_cached": 20, "tok_out": 3},
                             "result": {"k": [1, 2]}})
        self.assertIsNone(runmod.parse_line("  | pit: job=a"))


class Rank(unittest.TestCase):
    def test_critical_path_then_value_per_dollar(self):
        lg = L.MemLedger()
        add(lg, job("leaf-rich", value=5), job("leaf-poor", value=1),
            job("gate"), job("child", depends_on=["gate"]),
            job("large-leaf", lane="gpu-large", value=5, budget_usd=10))
        st = L.fold(lg.rows())
        # unblocking work first; then value per estimated $ (the funding, until past walls exist): 1/$1 outranks 5/$10
        self.assertEqual(L.rank(st, CFG), ["gate", "leaf-rich", "leaf-poor", "large-leaf"])


class Run(unittest.TestCase):
    def test_stop_rules(self):
        r = runmod.execute("echo start; echo 'dispatch feedback_report: 502s for 14 min'; sleep 5; echo never",
                           10, ["feedback_report"], echo=lambda *_: None)
        self.assertEqual(runmod.verdict_of(r)[0], "fail")
        self.assertIn("502s", runmod.verdict_of(r)[1])
        self.assertLess(r["wall_s"], 3)
        r = runmod.execute("echo 'prompt cache miss at call 2'", 10, ["cache_miss"], echo=lambda *_: None)
        self.assertEqual(runmod.verdict_of(r)[0], "fail")
        r = runmod.execute("sleep 5", 0.5, [], echo=lambda *_: None)
        self.assertEqual(runmod.verdict_of(r)[0], "fail")  # over budget fails; the partial trace is kept
        r = runmod.execute("""echo 'pit: verdict=fail wall_s=2 meters={"tok_in": 5}'; exit 0""", 10, [], echo=lambda *_: None)
        self.assertEqual((runmod.verdict_of(r)[0], r["report"]["meters"]), ("fail", {"tok_in": 5}))

    def test_shell_adapter_prints_a_report_line(self):
        r = runmod.execute(f"{ROOT}/examples/adapters/shell/run.sh sh -c 'echo a; echo b; exit 1'", 10, [], echo=lambda *_: None)
        self.assertEqual((runmod.verdict_of(r)[0], r["report"]["meters"], r["report"]["result"]), ("fail", {"out_lines": 2, "out_bytes": 4}, {"rc": 1}))

    def test_refusal_is_invalid(self):
        r = runmod.execute("exit 2", 10, [], echo=lambda *_: None)
        self.assertEqual(runmod.verdict_of(r)[0], "invalid")
        r = runmod.execute("echo 'pit: verdict=fail'; exit 2", 10, [], echo=lambda *_: None)
        self.assertEqual(runmod.verdict_of(r)[0], "fail")


def git_repo(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, check=True)


class Claim(unittest.TestCase):
    def test_push_race_loser_releases(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        remote, seed = tmp / "remote.git", tmp / "seed"
        subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
        subprocess.run(["git", "clone", "-q", str(remote), str(seed)], check=True, capture_output=True)
        _ident(seed)
        seed_lg = L.Ledger(seed / "ledger", "seed")
        add(seed_lg, job("j"))
        L.commit(seed, seed_lg, "add j")
        git_repo(seed, "push", "-q", "origin", "HEAD")
        hosts = {}
        for host in ("host-a", "host-b"):
            d = tmp / host
            subprocess.run(["git", "clone", "-q", str(remote), str(d)], check=True, capture_output=True)
            _ident(d)
            hosts[host] = (d, L.Ledger(d / "ledger", host))
        won, cid = L.claim(*hosts["host-a"], "j", "gpu-small", push_ok=True)
        self.assertTrue(won, cid)
        # host-b has not pulled: it sees j queued, claims, its push is rejected, it rebases, sees the rival, releases
        won, why = L.claim(*hosts["host-b"], "j", "gpu-small", push_ok=True)
        self.assertFalse(won)
        self.assertIn("lost the claim", why)
        d, lg = hosts["host-a"]
        git_repo(d, "pull", "-q", "--rebase")
        st = L.fold(lg.rows())
        self.assertTrue(st.jobs["j"]["claim"]["cid"].startswith("host-a:"))
        self.assertEqual(len(st.jobs["j"]["claims"]), 1)          # the loser's claim is released
        self.assertEqual(st.jobs["j"]["state"], "running")

    def repo(self, remote=None):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        subprocess.run(["git", "init", "-q", str(tmp)], check=True)
        _ident(tmp)
        if remote:
            git_repo(tmp, "remote", "add", "origin", str(tmp / "nowhere.git"))   # rejects every push
        lg = L.Ledger(tmp / "ledger", "h")
        add(lg, job("j", run="echo 'pit: verdict=pass'"))
        L.commit(tmp, lg, "add j")
        return tmp, lg

    def test_no_remote_runs_and_records(self):
        tmp, lg = self.repo()
        lines = []
        row = runmod.run_job(tmp, lg, CFG, "j", echo=lines.append)
        self.assertEqual(row["verdict"], "pass")
        self.assertIn("no remote: local lock", lines)

    def test_push_failure_releases(self):
        tmp, lg = self.repo(remote=True)
        with self.assertRaises(SystemExit) as e:
            runmod.run_job(tmp, lg, {**CFG, "git": {"push": True}}, "j", echo=lambda *_: None)
        self.assertIn("push kept failing", str(e.exception))
        rows = lg.rows()
        self.assertEqual([r["t"] for r in rows if r["t"] in ("claim", "release", "result")], ["claim", "release"])
        self.assertEqual(L.fold(rows).jobs["j"]["state"], "queued")

    def test_push_disabled_never_pushes(self):
        tmp, lg = self.repo(remote=True)
        with mock.patch.object(L, "git", wraps=L.git) as g:
            runmod.run_job(tmp, lg, CFG, "j", echo=lambda *_: None)
        self.assertFalse([c for c in g.call_args_list if c.args[1] == "push"])

    def test_q_run_refuses_a_full_lane_or_a_busy_device(self):
        """`q run` by hand is the same guard as autopilot: slots full, or another lane of the device has a run."""
        import copy
        tmp, lg = self.repo()
        cfg = copy.deepcopy(CFG)
        cfg["lanes"]["gpu-small"]["device"] = "gpu0"
        cfg["lanes"]["lens"] = {"usd_per_h": 2, "slots": 1, "gate": "true", "device": "gpu0"}
        add(lg, job("r", lane="lens", run="echo 'pit: verdict=pass'"))
        lg.append({"t": "claim", "job": "j", "lane": "gpu-small", "cid": "c-j"})
        with self.assertRaises(SystemExit) as e:
            runmod.run_job(tmp, lg, cfg, "r", echo=lambda *_: None)
        self.assertEqual(str(e.exception), "lane lens: device gpu0 busy (j running on gpu-small)")
        add(lg, job("j2", run="echo 'pit: verdict=pass'"))
        with self.assertRaises(SystemExit) as e:
            runmod.run_job(tmp, lg, cfg, "j2", echo=lambda *_: None)
        self.assertEqual(str(e.exception), "lane gpu-small: busy (j)")
        self.assertEqual([r["t"] for r in lg.rows() if r["t"] == "claim"], ["claim"])   # neither refused run claimed

    def test_killed_run_keeps_its_transcript_and_hands_back_the_tail(self):
        from pit import autopilot as A
        tmp, lg = self.repo()
        add(lg, job("k", run="i=0; while [ $i -lt 50 ]; do echo line$i; i=$((i+1)); done; sleep 5"))
        with mock.patch.object(specmod, "funded_seconds", return_value=1):
            row = runmod.run_job(tmp, lg, CFG, "k", echo=lambda *_: None)
        self.assertEqual(row["verdict"], "fail")
        self.assertTrue(row["note"].startswith("over budget"))
        self.assertTrue(row["log"].startswith("autopilot/logs/run-k-"))
        self.assertIn("line49", (tmp / row["log"]).read_text())
        self.assertEqual(row["tail"].splitlines(), [f"line{i}" for i in range(10, 50)])
        text = A.finished("k", L.fold(lg.rows()).jobs["k"])
        self.assertIn(f"The run log (everything it printed): {row['log']}", text)
        self.assertIn("What your run did before it stopped (last 40 lines):\nline10\n", text)
        self.assertIn("A run killed for funding still hands you everything it printed; read it before you re-post.", text)


def _ident(d):
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        git_repo(d, "config", k, v)


class Replay(unittest.TestCase):
    def test_example_night(self):
        jobs, t0, sim, st, text = replay.run(EXAMPLE, EXAMPLE)
        self.assertEqual(len(jobs), 11)
        self.assertTrue(sim["sweep-large"]["stopped"])                  # the feedback_report stop rule
        self.assertNotIn("ablate-b", sim)                               # stale after the leak check, never claimed
        self.assertEqual(st.stale["ablate-b"]["by"], "leak-check")
        self.assertEqual(st.findings["F:b-beats-a"]["status"], "refuted")
        self.assertEqual(st.findings["F:leak"]["status"], "open")
        self.assertLess(sim["seed-sweep"]["start"], sim["leak-check"]["end"])  # Pit: matched variant-b first, then the cheapest
        self.assertGreater(sim["seed-sweep"]["start"], sim["variant-b"]["start"])
        hand = sum(a["usd"] for s, a in jobs.values() if a.get("hand_run", True))
        self.assertAlmostEqual(hand - sum(v["usd"] for v in sim.values()), 26.0)
        self.assertIn("critical path wall", text)
        self.assertIn("wallets after settlement: explorer $90.60, skeptic $105.56, house $1.11", text)

    def test_example_ledger_is_the_replay(self):
        """examples/replay-synthetic/ledger is `q replay --out`: fold, Pit settlement, calibration, reflect and view on it."""
        rows = L.Ledger(EXAMPLE / "ledger").rows()
        sim_rows = replay.simulate(*replay.load(EXAMPLE), CFG)[2].rows()
        self.assertEqual([(r["t"], r.get("id") or r.get("job")) for r in rows], [(r["t"], r.get("id") or r.get("job")) for r in sim_rows])
        book = B.Book(rows)
        self.assertEqual(len(book.settled), 4)                          # variant-b, seed-sweep, leak-check, sweep-large
        self.assertEqual(book.settled[("sweep-large", "main")]["payouts"], {"house": 1.0})   # no winner: the house keeps it
        self.assertAlmostEqual(book.balances()["house"], 1.115)
        cal = {l.split()[0]: l.split() for l in B.calibration(rows).splitlines()[1:]}
        self.assertEqual((cal["explorer"][1], cal["skeptic"][1]), ("2", "1"))
        self.assertEqual([r["t"] for r in rows].count("reflect"), 1)
        self.assertEqual(reflect.since_last(rows), [])
        st = L.fold(rows)
        self.assertEqual(len(st.findings), 2)
        self.assertIn(("leak-check", "refutes", "F:b-beats-a"), {(e["from"], e["type"], e["to"]) for e in st.all_edges()})
        self.assertEqual({k: round(book.totals("variant-b", "main")[k], 2) for k in B.SIDES}, {"pass": 0.25, "fail": 3.0})


class Hooks(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        shutil.copy(ROOT / "lanes.example.toml", self.root / "lanes.toml")
        # the replayed night is this root's ledger, plus one queued job so the frontier is not empty
        _, _, sim_lg = replay.simulate(*replay.load(EXAMPLE), CFG)
        (self.root / "ledger").mkdir()
        (self.root / "ledger" / "sim.ndjson").write_text("".join(json.dumps(r) + "\n" for r in sim_lg.rows()))
        self.lg = L.Ledger(self.root / "ledger", "local")
        add(self.lg, job("next-rung", lane="gpu-large", budget_usd=9,
                         if_fail="ask the owner whether to ship v2"))

    def test_session_start_script(self):
        env = {**os.environ, "PIT_ROOT": str(self.root)}
        out = subprocess.run(["sh", str(ROOT / "scripts" / "py.sh"), "-m", "pit.hooks", "session-start"],
                             input='{"session_id": "s1"}', capture_output=True, text=True, env=env, check=True).stdout
        lines = out.strip().splitlines()
        self.assertLessEqual(len(lines), 20)
        self.assertTrue(lines[0].startswith("Pit: 1 runnable"))
        self.assertIn("desk  next-rung", out)      # no run/scenario: a desk job, not runnable
        self.assertIn("STALE ablate-b: F:b-beats-a refuted by leak-check", out)
        self.assertIn("spend today:", out)

    def test_stop_notifies_on_human_branch(self):
        hooks.session_start(self.root, {"session_id": "s2"})
        self.lg.append({"t": "result", "job": "next-rung", "verdict": "fail",
                        "cost": lanes.cost_line(CFG, "gpu-large", wall_s=300)})
        items = hooks.news_since(self.root, json.loads(hooks._mark(self.root, "s2").read_text())["ts"])
        self.assertEqual(items, ["next-rung FAIL -> ask the owner whether to ship v2"])

    def test_post_tool_use_ledgers_pit_line(self):
        os.environ["PIT_HOST"] = "local"
        self.addCleanup(os.environ.pop, "PIT_HOST")
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        hook = {"tool_name": "Bash", "tool_response": {"stdout": "x\npit: job=next-rung verdict=pass wall_s=120\n"}}
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rows = hooks.post_tool_use(self.root, hook)
        self.assertIn("reflect due", out.getvalue())                        # the last reflect row is hours old: the hook nudges
        self.assertEqual(rows[0]["verdict"], "pass")
        self.assertAlmostEqual(rows[0]["cost"]["usd"], 120 / 3600 * 100, places=3)
        self.assertEqual(hooks.post_tool_use(self.root, hook), [])        # already recorded: no double row


class Reflect(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        shutil.copy(ROOT / "lanes.example.toml", self.root / "lanes.toml")
        self.lg = L.Ledger(self.root / "ledger", "local")
        add(self.lg, job("a"), job("b", depends_on=["a"]))
        self.lg.append({"t": "result", "job": "a", "verdict": "pass", "cost": lanes.cost_line(CFG, "gpu-small", wall_s=90)})
        self.lg.append({"t": "node", "kind": "hypothesis", "id": "F:h", "from": "a", "text": "waits repeat"})
        self.lg.append({"t": "decision", "finding": "F:h", "changed": True, "note": "moved on"})

    def test_digest(self):
        d = reflect.digest(self.lg.rows())
        self.assertIn("does a hold?", d)
        self.assertIn("-> pass", d)
        self.assertIn("hypothesis F:h (from a): waits repeat", d)
        self.assertIn("decision F:h CHANGED", d)
        self.assertIn("spend by lane: gpu-small $", d)
        self.assertIn("frontier: b", d)
        self.assertIn('"id": "b"', d)          # open job's full spec

    def test_counter_resets_and_fold_ignores(self):
        before = L.fold(self.lg.rows())
        self.assertEqual(len(reflect.since_last(self.lg.rows())), 5)
        self.lg.append({"t": "reflect", "note": "n", "rows_covered": 5})
        rows = self.lg.rows()
        self.assertEqual(reflect.since_last(rows), [])
        self.assertEqual(sorted(L.fold(rows).jobs), sorted(before.jobs))
        self.assertEqual(L.fold(rows).frontier(), before.frontier())
        self.assertIn("0 rows since last reflection", hooks.status_block(self.root))

    def test_nudge_once_per_reason_and_count(self):
        self.assertFalse(reflect.nudge(self.root, None, 63))
        self.assertTrue(reflect.nudge(self.root, "rows 64", 64))
        self.assertFalse(reflect.nudge(self.root, "rows 64", 64))
        self.assertTrue(reflect.nudge(self.root, "rows 65", 65))
        self.assertTrue(reflect.nudge(self.root, "after cancel", 65))
        for i in range(60):
            self.lg.append({"t": "decision", "id": "a", "note": str(i)})
        self.assertIn("reflect due (rows 65): /pit:reflect", hooks.status_block(self.root))

    def test_predicates(self):
        now = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)
        row = lambda t, h=0, **k: {"t": t, "ts": (now - timedelta(hours=h)).isoformat(), **k}
        res = lambda *usd: [row("result", job=f"j{i}", verdict="pass", cost={"usd": u}) for i, u in enumerate(usd)]
        mt = lambda rs: {r["job"]: {"cost": r["cost"]["usd"]} for r in rs if r["t"] == "result" and "cost" in r}
        P = {k: (lambda f: lambda rs, now, v: f(rs, mt(rs), now, v))(f) for k, f in reflect.PREDICATES.items()}
        self.assertEqual(P["rows"](res(1, 1), now, 2)[0], "rows 2")
        self.assertIsNone(P["rows"](res(1), now, 2)[0])
        self.assertEqual(P["hours"]([row("review", 9)], now, 8)[0], "hours 9.0")
        self.assertIsNone(P["hours"]([row("review", 1)], now, 8)[0])
        self.assertIsNone(P["hours"]([], now, 8)[0])
        self.assertEqual(P["after"]([row("cancel")], now, ["cancel"])[0], "after cancel")
        self.assertEqual(P["after"]([row("result", verdict="invalid")], now, ["result:invalid"])[0], "after result:invalid")
        self.assertEqual(P["after"]([row("node", kind="hypothesis")], now, ["node:hypothesis"])[0], "after node:hypothesis")
        self.assertIsNone(P["after"](res(1), now, ["cancel", "result:fail"])[0])
        self.assertEqual(P["spend_rising"](res(2.09, 5.10, 12.40), now, 3)[0], "spend rising 3 results: $2.09 → $5.10 → $12.40")
        self.assertIsNone(P["spend_rising"](res(9, 2, 5), now, 3)[0])
        self.assertIsNone(P["spend_rising"](res(2, 2, 3), now, 3)[0])

    def test_due_first_reason_and_defaults(self):
        rows = self.lg.rows()
        now = datetime.fromisoformat(rows[0]["ts"])
        self.assertIsNone(reflect.due(rows, now, {}))                                  # defaults: nothing fires
        self.assertEqual(reflect.due(rows, now, {"reflect": {"rows": 3, "after": ["decision"]}}), "rows 5")
        self.assertEqual(reflect.due(rows, now, {"reflect": {"after": ["decision"], "rows": 3}}), "after decision")
        self.assertIsNone(reflect.due(rows, now, {"reflect": {}}))                     # every predicate off
        self.assertEqual(reflect.due(rows, now + timedelta(hours=9), {}), "hours 9.0")

    def test_metrics(self):
        lg = L.Ledger(self.root / "ledger2", "local")
        add(lg, job("a", budget_usd=2), job("b", depends_on=["a"], budget_usd=4),
            job("c", depends_on=["b"]))
        lg.append({"t": "result", "job": "a", "verdict": "pass", "cost": {"usd": 1.0, "wall_s": 50.0, "lane": "any"}})
        lg.append({"t": "result", "job": "b", "verdict": "pass", "cost": {"usd": 6.0, "wall_s": 5.0, "lane": "any"}})
        lg.append({"t": "node", "kind": "hypothesis", "id": "F:h", "from": "b", "text": "x"})
        m = metrics(lg.rows())
        self.assertEqual((m["a"]["cost_ratio"], m["a"]["depth"], m["a"]["lineage_spend"]), (0.5, 0, 1.0))
        self.assertEqual((m["b"]["cost_ratio"], m["b"]["depth"], m["b"]["lineage_spend"]), (1.5, 1, 7.0))
        self.assertEqual((m["c"]["depth"], m["c"]["lineage_spend"], m["c"]["verdict"]), (2, 7.0, None))
        self.assertEqual(m["a"]["rows_since_added"], 3)
        self.assertEqual(m["F:h"]["open_dependents"], 0)
        self.assertIn("lineage_spend", table(m))
        # a predicate that reads metrics: spend_rising takes costs from m, not from the rows
        rs = [{"t": "result", "job": j, "ts": "2026-01-01T00:00:00+00:00"} for j in "ab"]
        self.assertIn("$1.00 → $6.00", reflect.PREDICATES["spend_rising"](rs, m, None, 2)[0])
        self.assertIsNone(reflect.PREDICATES["spend_rising"](rs, m, None, 3)[0])
        self.assertIn("lineage_spend", reflect.digest(lg.rows()))

    def test_rerecorded_result_counts_once(self):
        lg = L.Ledger(self.root / "ledger3", "local")
        add(lg, job("a"))
        for usd in (30.0, 30.0):
            lg.append({"t": "result", "job": "a", "verdict": "pass", "cost": {"usd": usd, "wall_s": 1.0, "lane": "gpu-small"}})
        self.assertEqual(L.fold(lg.rows()).spend(), {"gpu-small": 30.0})
        self.assertEqual(metrics(lg.rows())["a"]["cost"], 30.0)
        self.assertIn("spend by lane: gpu-small $30.00", reflect.digest(lg.rows()))

    def test_why_output(self):
        rows = self.lg.rows()
        out = reflect.why(rows, datetime.fromisoformat(rows[0]["ts"]), {})
        self.assertIn("rows: 5/64", out)
        self.assertIn("hours: 0.0/8", out)
        self.assertIn("after cancel|result:invalid: none", out)
        self.assertIn("spend_rising: 1/3", out)
        r = subprocess.run([sys.executable, "-m", "pit.cli", "reflect", "--why"], cwd=ROOT, capture_output=True,
                           text=True, env={**os.environ, "PIT_ROOT": str(self.root)})
        self.assertIn("rows: 5/64", r.stdout, r.stderr)
        self.assertIn("spend_rising: 1/3", r.stdout)


class Root(unittest.TestCase):
    def test_root_flag_env_and_missing_lanes(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        shutil.copy(ROOT / "lanes.example.toml", tmp / "lanes.toml")
        lg = L.Ledger(tmp / "ledger", "local")
        add(lg, job("only-here"))
        env = {k: v for k, v in os.environ.items() if k != "PIT_ROOT"}
        q = lambda *a, **kw: subprocess.run([sys.executable, "-m", "pit.cli", *a], cwd=kw.get("cwd", ROOT),
                                            capture_output=True, text=True, env={**env, **kw.get("env", {})})
        self.assertIn("only-here", q("--root", str(tmp), "list").stdout)
        self.assertIn("only-here", q("list", env={"PIT_ROOT": str(tmp)}).stdout)
        self.assertIn("only-here", q("list", cwd=tmp / "ledger", env={"PYTHONPATH": str(ROOT)}).stdout)   # nearest parent
        (tmp / "empty").mkdir()
        r = q("--root", str(tmp / "empty"), "status")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no lanes.toml", r.stderr)


class View(unittest.TestCase):
    def setUp(self):
        self.lg = L.MemLedger()
        T = "2026-09-28T01:00:0%dZ"
        add(self.lg, job("a", produces_if_pass=["F:x"]), job("b", depends_on=["a"]), job("c"), job("d"), ts=T % 0)
        self.lg.append({"t": "claim", "job": "a", "lane": "gpu-small", "cid": "sim:1"}, T % 1)
        self.lg.append({"t": "result", "job": "a", "verdict": "pass", "cost": {"usd": 2.0, "wall_s": 5.0, "lane": "gpu-small"}}, T % 2)
        L.settle(self.lg, self.lg.rows()[0]["spec"], "pass", T % 3)
        self.lg.append({"t": "cancel", "id": "d", "reason": "dead"}, T % 4)
        self.lg.append({"t": "reflect", "note": "n"}, T % 5)
        self.rows = self.lg.rows()
        self.now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)

    def test_server(self):
        import threading, urllib.error, urllib.request
        srv = view.ThreadingHTTPServer(("127.0.0.1", 0), view.make_handler(lambda: self.rows, CFG))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_port}"
        try:
            full = json.load(urllib.request.urlopen(base + "/market.json"))
            cut = json.load(urllib.request.urlopen(base + "/market.json?upto=1"))
            self.assertEqual((full["rows"], full["total_rows"], cut["rows"]), (10, 10, 1))
            self.assertEqual([m["task"] for m in cut["markets"]], ["a"])
            page = urllib.request.urlopen(base + "/").read()
            self.assertIn(b"market.json", page)
            self.assertEqual(page, urllib.request.urlopen(base + "/terminal").read())
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(base + "/graph.json")
            self.assertEqual(e.exception.code, 404)
        finally:
            srv.shutdown()
            srv.server_close()


class OverBudget(unittest.TestCase):
    def test_timeout_is_a_fail_with_partial_trace_kept(self):
        from pit.run import verdict_of
        v, note = verdict_of({"stop": "timeout", "stop_text": "2x budget at 600s", "report": {}, "rc": -9})
        self.assertEqual(v, "fail"); self.assertIn("partial trace kept", note)


class SilentRun(unittest.TestCase):
    def test_exit_zero_without_verdict_is_invalid(self):
        from pit.run import verdict_of
        self.assertEqual(verdict_of({"stop": None, "report": {}, "rc": 0})[0], "invalid")

    def test_version(self):
        r = subprocess.run([sys.executable, "-m", "pit.cli", "--version"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), "pit 0.5.0")


class Pit(unittest.TestCase):
    """PIT.md: income, posts, bets, matched-stake ranking, parimutuel settlement, thread, calibration."""
    PCFG = {**CFG, "pit": {"enabled": True, "default_stake": 0.25, "vig_rate": 0.02}}
    T0 = datetime(2026, 9, 29, 4, 30, tzinfo=timezone.utc)

    def setUp(self):
        self.lg = L.MemLedger()
        book = B.Book([])
        for aid, parent in (("a", None), ("b", None), ("a.sub", "a")):
            book.agents[aid] = self.lg.append(B.agent_row(book, aid, f"{aid} brief", parent), "2026-09-29T04:00:00Z")
        B.tick(self.lg, self.PCFG, self.T0, since="2026-09-29T04:00:00Z")      # 30 min x $214/h = $107

    def book(self):
        return B.Book(self.lg.rows())

    def post(self, spec, agent, seed=False, stake=0.0, cfg=None, ts="2026-09-29T04:31:00Z"):
        """What `q post` does, in memory. Returns the funding mode."""
        mode = B.funding(self.book(), spec, agent, seed)
        if mode == "agent":
            self.assertIsNone(B.check_post(self.book(), cfg or self.PCFG, spec, agent))
        add(self.lg, {**spec, "proposer": agent, **({"seed": True} if mode == "seed" else {})}, ts=ts)
        for row in B.stakes(self.lg.rows(), cfg or self.PCFG, spec, agent, mode, stake):
            self.lg.append(row, ts)
        return mode

    def settle(self, jid, verdict, ts="2026-09-29T05:00:00Z"):
        self.lg.append({"t": "result", "job": jid, "verdict": verdict, "cost": {"usd": 1, "wall_s": 1, "lane": "x"}}, ts)
        return B.settle_due(self.lg, self.PCFG)

    def house_with_vig(self):
        self.post(job("x"), "a")
        self.bet("x", "fail", 9.75, "b")                                         # pot 10: vig 0.20 to the house
        self.settle("x", "fail")
        return self.book().balances()["house"]

    def test_house_pool_grows_by_vig(self):
        self.assertNotIn("house", self.book().balances())
        self.assertAlmostEqual(self.house_with_vig(), 0.20)
        self.post(job("y"), "a")
        self.bet("y", "pass", 1, "b")
        self.settle("y", "pass", "2026-09-29T05:10:00Z")                         # pot 1.25: vig 0.025
        self.assertAlmostEqual(self.book().balances()["house"], 0.225)

    def test_reflection_plants_an_agent_with_a_house_seeded_root(self):
        from argparse import Namespace as N
        from pit import cli, market
        self.house_with_vig()
        self.lg.append(B.agent_row(self.book(), "reflect", "the shape of the population"))
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        with mock.patch.object(cli, "ctx", lambda: (root, self.lg, self.PCFG)), mock.patch.object(cli, "sync", lambda *a: None):
            cli.cmd_agent(N(verb="add", id="c", brief="a capability several agents lacked, tested without its instructions",
                            parent=None, reason=None, by="reflect", runtime=None, model=None))
        self.assertEqual((self.book().agents["c"]["kind"], self.book().agents["c"]["by"]), ("persistent", "reflect"))
        self.assertIn("a capability several agents lacked", (root / "agents" / "c.toml").read_text())
        self.assertEqual(self.post(job("c-root"), "reflect"), "seed")                # its first experiment: a root reflect posts
        self.assertEqual([(b["agent"], b["tags"]) for b in self.book().bets if b["job"] == "c-root"], [("house", ["seed"])])
        self.assertIn("c", self.book().active())
        self.assertIn("c", {a["id"] for a in market.market_json(self.lg.rows(), self.PCFG, self.T0)["agents"]})

    def test_reflect_root_is_house_seeded(self):
        self.house_with_vig()
        book = B.Book([])
        for aid, parent in (("reflect", None), ("reflect.p1", "reflect")):
            book.agents[aid] = self.lg.append(B.agent_row(book, aid, "reflection", parent), "2026-09-29T04:05:00Z")
        before = self.book().balance("reflect")
        cfg = {**self.PCFG, "pit": {**self.PCFG["pit"], "house_seed": 0.15}}
        self.assertEqual(self.post(job("r1", budget_usd=5, arms=["V1", "V2"]), "reflect.p1", cfg=cfg), "seed")
        seeds = [b for b in self.book().bets if b["job"] == "r1"]
        # 0.15 on V1 (house_seed), then the 0.05 left in the pool on V2
        self.assertEqual([(b["agent"], b["variant"], b["usd"], b["tags"]) for b in seeds],
                         [("house", "V1", 0.15, ["seed"]), ("house", "V2", 0.05, ["seed"])])
        self.assertAlmostEqual(self.book().balances()["house"], 0.0)
        self.assertAlmostEqual(self.book().balance("reflect"), before)          # no budget debit, no stake
        self.assertEqual(self.post(job("r2"), "reflect", cfg=cfg), "seed")     # the pool is empty: no seed
        self.assertEqual([b for b in self.book().bets if b["job"] == "r2"], [])

    def test_a_lane_can_turn_the_house_seed_off(self):
        self.house_with_vig()
        book = B.Book([])
        book.agents["reflect"] = self.lg.append(B.agent_row(book, "reflect", "reflection", None), "2026-09-29T04:05:00Z")
        lane = job("r1")["lane"]
        cfg = {**self.PCFG, "lanes": {**self.PCFG["lanes"], lane: {**self.PCFG["lanes"][lane], "house_seed": 0}}}
        self.assertEqual(self.post(job("r1", budget_usd=5), "reflect", cfg=cfg), "seed")
        self.assertEqual([b for b in self.book().bets if b["job"] == "r1"], [])   # this lane's roots get no house money
        self.assertGreater(self.book().balances()["house"], 0)                     # the pool is untouched
        self.assertEqual(B.funding(self.book(), job("r3", depends_on=["r1"]), "reflect"), "agent")   # not a root
        self.assertEqual(B.funding(self.book(), job("r4", **{"from": "F:x"}), "reflect"), "agent")
        self.assertEqual(B.funding(self.book(), job("r5"), "a", seed=True), "seed")                 # --seed

    def test_human_and_agent_roots_are_not_seeded(self):
        self.house_with_vig()
        self.assertEqual(self.post(job("h1", budget_usd=40), "human", stake=2), "human")
        self.assertEqual(self.post(job("h2"), "human"), "human")
        b = self.book()
        self.assertEqual([(x["agent"], x["usd"], x["tags"]) for x in b.bets if x["job"] in ("h1", "h2")],
                         [("human", 2.0, ["human"])])
        self.assertAlmostEqual(b.flows["human"], -2.0)                           # its stake only, never the budget
        a_before = b.balance("a")
        self.assertEqual(self.post(job("g1", budget_usd=3), "a"), "agent")
        b = self.book()
        self.assertAlmostEqual(b.balance("a"), a_before - 3.75)                 # its wallet pays: budget + 25% of it
        self.assertEqual([x["agent"] for x in b.bets if x["job"] == "g1"], ["a"])
        self.assertAlmostEqual(b.balances()["house"], 0.20)                     # untouched

    def test_seeds_excluded_from_calibration(self):
        self.house_with_vig()
        self.post(job("s"), "a", seed=True)                                       # house PASS 0.20
        self.bet("s", "fail", 1, "b")
        self.settle("s", "fail", "2026-09-29T05:20:00Z")
        cal = {l.split()[0]: l.split() for l in B.calibration(self.lg.rows()).splitlines()[1:]}
        self.assertNotIn("house", cal)
        self.assertEqual(cal["b"][1:3], ["2", "2"])                              # x and s, both won
        self.assertEqual(cal["b"][6], "0%")                                       # against the seed's side: not herding

    def test_sleep_wakes_once_per_condition(self):
        self.post(job("m"), "b")
        self.post(job("open"), "b")
        at = lambda i, k: B.iso(self.T0 + timedelta(minutes=60 + 20 * i + k))
        cases = [("a", {"balance": 60}, lambda i: B.tick(self.lg, self.PCFG, B.parse_t(at(i, 5)))),   # 53.5 + drip
                 ("a", {"result": "m"}, lambda i: self.settle("m", "pass", at(i, 5))),
                 ("b", {"market": "open"}, lambda i: self.bet("open", "fail", 1, "a", ts=at(i, 5))),
                 ("a", {"minutes": 7}, lambda i: None)]
        for i, (agent, until, trigger) in enumerate(cases):
            self.lg.append(B.sleep_row(self.book(), agent, until, "waiting"), at(i, 0))
            self.assertEqual(B.wake(self.lg, B.parse_t(at(i, 1))), [], until)          # not yet
            trigger(i)
            woke = B.wake(self.lg, B.parse_t(at(i, 8)))
            self.assertEqual([w["agent"] for w in woke], [agent], until)
            self.assertEqual(B.wake(self.lg, B.parse_t(at(i, 9))), [], until)          # exactly once
            self.assertIn("awake: ", B.thread(self.lg.rows(), agent))
            self.bet("open", "pass", 0.5, agent, ts=at(i, 10))                               # its next act ends the sleep
            self.assertNotIn(agent, B.sleepers(self.lg.rows()))
        with self.assertRaises(SystemExit):
            B.sleep_row(self.book(), "a", {}, "no condition")

    def test_thread_shows_sleep(self):
        self.lg.append(B.sleep_row(self.book(), "a", {"balance": 99, "result": "x"}, "the run I want costs $99"),
                       "2026-09-29T04:40:00Z")
        out = B.thread(self.lg.rows(), "a")
        self.assertIn("sleeping until balance $99.00, x has a result (since 04:40Z): the run I want costs $99", out)
        self.assertNotIn("sleeping", B.thread(self.lg.rows(), "b"))

    def bet(self, job, side, usd, agent, variant="main", ts="2026-09-29T04:32:00Z", **kw):
        rows = self.lg.rows()
        return self.lg.append(B.bet_row(B.Book(rows), L.fold(rows), job, variant, side, usd, agent, **kw), ts)

    def test_drip_split_and_idempotent(self):
        d = [r for r in self.lg.rows() if r["t"] == "drip"][0]
        self.assertEqual(d["minutes"], 30)
        self.assertEqual(d["to"], {"a": 53.5, "b": 53.5})                        # subs get nothing
        self.assertIsNone(B.tick(self.lg, self.PCFG, self.T0 + timedelta(seconds=59)))
        r = B.tick(self.lg, self.PCFG, self.T0 + timedelta(seconds=150))
        self.assertEqual((r["minutes"], r["until"]), (2, "2026-09-29T04:32:00.000Z"))   # leftover seconds carry over
        self.assertAlmostEqual(self.book().balance("a"), 53.5 + 214 / 60, places=3)

    def test_post_debits_and_auto_stakes(self):
        self.post(job("x", budget_usd=10), "a.sub")               # a sub spends from its parent
        b = self.book()
        self.assertAlmostEqual(b.balance("a"), 53.5 - 10 - 2.5)                 # auto stake: 25% of $10 funding
        self.assertEqual((b.bets[0]["side"], b.bets[0]["usd"], b.bets[0]["tags"]), ("pass", 2.5, ["auto", "self"]))
        self.post(job("small", budget_usd=0.4), "b")                             # 25% of $0.40 is under the floor
        self.assertEqual(self.book().totals("small", "main")["pass"], 0.25)
        self.assertEqual((b.bets[0]["side"], b.bets[0]["book"]), ("pass", "a"))
        self.assertIn("needs", B.check_post(b, self.PCFG, job("y", budget_usd=500, lane="ci"), "b"))

    def test_new_markets_since_last_turn(self):
        self.post(job("old"), "b")
        since = len(self.lg.rows())
        self.post(job("x", lane="ci", question="does the small model pass multi-step tasks?"), "b")
        self.post(job("mine"), "a.sub")                                          # a's own post: not listed
        self.post(job("done"), "b")
        self.bet("done", "fail", 0.5, "a.sub")                                   # already bet on by a: not listed
        seen = {**self.PCFG, "pit": {**self.PCFG["pit"], "blind": False}}
        text = B.new_markets(self.lg.rows(), seen, "a", since)
        self.assertIn("x [ci] does the small model pass multi-step tasks? · funded $1 (36s) · PASS $0.25 / FAIL $0.00 · $1 on FAIL pays $1.23 · "
                      "proposer b (0-0 on posts, 0-0 on bets)", text)
        self.assertTrue(text.startswith("New markets since your last turn:\n") and text.endswith(B.NEW_RULE))
        for jid in ("old", "mine", "done"):
            self.assertNotIn(f"\n{jid} [", text)
        self.assertEqual(B.new_markets(self.lg.rows(), seen, "b", since).count("\nmine ["), 1)
        for i in range(11):
            self.post(job(f"m{i}"), "b")
        more = B.new_markets(self.lg.rows(), self.PCFG, "a", since)
        self.assertIn("\nm10 [", more)
        self.assertIn("… 2 more: q board", more)                                  # 12 open (x + m0..m10), 10 shown

    def test_blind_agent_views_carry_no_market_information(self):
        self.post(job("x", question="does x hold?"), "b")
        self.bet("x", "fail", 1, "a", why="x breaks on held-out input")
        self.post(job("y"), "a")
        self.bet("y", "pass", 0.5, "b", why="y looks fine")
        rows, blind, seen = self.lg.rows(), self.PCFG, {**self.PCFG, "pit": {**self.PCFG["pit"], "blind": False}}
        ev = list(range(len(rows)))
        leaks = ("PASS $", "FAIL $", "pays", "market moved", "held-out input", "looks fine", "book PASS")
        views = lambda cfg, hide: [B.board(rows, cfg, hide=hide), B.new_markets(rows, cfg, "a", 0),
                                   B.digest(rows, ev, cfg), B.thread(rows, "a", hide)]
        for text in views(blind, True)[:3]:
            self.assertFalse([k for k in leaks if k in text], text)
        self.assertNotIn("book PASS", views(blind, True)[3])                           # the thread keeps only its own stakes
        self.assertIn("x [gpu-small] does x hold? · funded $1 (257s) · proposer b (0-0 on posts, 0-0 on bets)", B.board(rows, blind, hide=True))
        self.assertIn("x/main FAIL $1.00", B.thread(rows, "a", True))                # its own stake stays visible
        full = "\n".join(views(seen, False))
        for k in ("PASS $", "pays", "held-out input", "book PASS"):
            self.assertIn(k, full)
        self.assertEqual(B.board(rows, blind), B.board(rows, seen))                  # the human board is unchanged
        strip = lambda m: {k: v for k, v in m.items() if k != "generated_at"}      # wall-clock stamp differs between the two calls
        self.assertEqual(strip(market.market_json(rows, blind, self.T0)), strip(market.market_json(rows, seen, self.T0)))

    def test_bet_escrow_sub_booking_self_tag(self):
        self.post(job("x", arms=["V1", "V2"]), "a")
        r = self.bet("x", "fail", 2, "b", "V2")
        self.assertEqual((r["book"], r["tags"]), ("b", []))
        self.assertEqual(self.bet("x", "pass", 1, "a.sub", "V1")["tags"], ["self"])   # the proposer's own sub
        self.assertAlmostEqual(self.book().balance("b"), 51.5)
        self.assertEqual(self.book().totals("x", "V2"), {"pass": 0.25, "fail": 2.0})
        with self.assertRaises(SystemExit):
            self.bet("x", "fail", 1, "b", "V9")
        with self.assertRaises(SystemExit):
            self.bet("x", "fail", 999, "b", "V1")
        self.lg.append({"t": "claim", "job": "x", "lane": "gpu-small", "cid": "c1"}, "2026-09-29T04:33:00Z")
        with self.assertRaises(SystemExit):
            self.bet("x", "fail", 1, "b", "V1")                                   # betting closed at claim

    def cli(self, fn, **kw):
        from argparse import Namespace as N
        from pit import cli
        with mock.patch.object(cli, "ctx", lambda: (ROOT, self.lg, self.PCFG)), mock.patch.object(cli, "sync", lambda *a: None):
            return getattr(cli, fn)(N(**kw))

    def test_read_is_funded_with_no_market(self):
        from pit import autopilot as A
        rd = job("rd", kind="read", then="post the rollout the reading points at", if_pass="", if_fail="")
        self.assertEqual(specmod.validate(rd, CFG["lanes"]), [])                          # `then`, not if_pass/if_fail
        self.assertIn("missing then: what you will do with the reading", specmod.validate({**rd, "then": ""}, CFG["lanes"]))
        self.post(job("x"), "a")
        self.bet("x", "fail", 1, "b", why="no")
        self.settle("x", "pass")
        recs = B.records(self.book())
        before = self.book().balance("b")
        self.assertEqual(self.post(rd, "b"), "agent")
        self.assertEqual(B.funding(self.book(), rd, "reflect"), "agent")                 # never house-seeded
        self.assertEqual([r for r in self.lg.rows() if r["t"] == "bet" and r["job"] == "rd"], [])   # no stake, no seed
        self.assertAlmostEqual(self.book().balance("b"), before - 1)                     # the budget only
        with self.assertRaisesRegex(SystemExit, "reads have no market"):
            self.bet("rd", "pass", 1, "a", why="x")
        self.assertNotIn("rd", B.board(self.lg.rows(), self.PCFG))
        row = runmod.record(self.lg, self.PCFG, L.fold(self.lg.rows()).jobs["rd"]["spec"], "gpu-small",
                            {"report": {"verdict": "read", "result": {"readout": "reads/rd.md"}}, "wall_s": 60.0, "rc": 0, "output": "x"})
        self.assertEqual((row["verdict"], row["result"]), ("read", {"readout": "reads/rd.md"}))
        self.assertNotIn("tail", row)
        self.assertAlmostEqual(row["funding"]["usd"], 1 - row["cost"]["usd"])            # the unspent part back
        self.assertAlmostEqual(self.book().balance("b"), before - row["cost"]["usd"])
        self.assertFalse(any(r["t"] == "settle" and r["job"] == "rd" for r in self.lg.rows()))
        self.assertEqual(B.records(self.book()), recs)                                   # neither a win nor a loss
        self.assertEqual(A.finished("rd", L.fold(self.lg.rows()).jobs["rd"]).split(" cost")[0],
                         "Your read is in: reads/rd.md; write what it makes you expect, as a finding, before you post a rollout.")
        silent = runmod.record(self.lg, self.PCFG, rd, "gpu-small", {"report": {}, "wall_s": 1.0, "rc": 0})
        self.assertEqual(silent["verdict"], "invalid")                                   # a driver that never reported
        self.assertEqual(L.fold(self.lg.rows()).dep_reason("rd"), "rd ended invalid: not evidence; rerun or cancel")

    def test_edge_refuses_missing_node(self):
        self.post(job("x"), "a")
        with self.assertRaises(SystemExit):
            self.cli("cmd_edge", src="x", type="supersedes", dst="ghost")
        self.assertEqual([r for r in self.lg.rows() if r["t"] == "edge"], [])

    def test_proposer_desk_zero_result_voids_its_own_bet(self):
        self.post(job("x"), "a")                                                 # a stakes PASS 0.25
        self.bet("x", "fail", 1, "b")
        before = self.book().balance("a")
        self.lg.append({"t": "result", "job": "x", "verdict": "pass", "agent": "a",
                        "cost": {"usd": 0, "wall_s": 0, "lane": "gpu-small"}}, "2026-09-29T05:00:00Z")
        B.settle_due(self.lg, self.PCFG)
        self.assertAlmostEqual(self.book().balance("a"), before + 0.25)          # refunded, not paid the pot
        (s,) = [r for r in self.lg.rows() if r["t"] == "settle"]
        self.assertTrue(s["void_self"])

    def test_result_refuses_dead_branch_without_force(self):
        self.post(job("r"), "a")
        self.post(job("cut", depends_on=["r@pass"]), "a")
        self.lg.append({"t": "result", "job": "r", "verdict": "fail", "cost": {"usd": 0, "wall_s": 0, "lane": "gpu-small"}})
        kw = dict(id="cut", verdict="pass", wall_s=0.0, meter=[], lane=None, arm=None, agent=None)
        with self.assertRaises(SystemExit):
            self.cli("cmd_result", force=False, **kw)
        self.cli("cmd_result", force=True, **kw)
        self.assertIsNotNone(L.fold(self.lg.rows()).jobs["cut"]["result"])

    def test_verdict_only_correction_inherits_cost(self):
        self.post(job("c"), "a")
        kw = dict(id="c", meter=[], lane=None, arm=None, agent=None, force=False)
        self.cli("cmd_result", verdict="pass", wall_s=1800.0, **kw)
        first = L.fold(self.lg.rows()).jobs["c"]["result"]["cost"]["usd"]
        self.assertGreater(first, 0)
        self.cli("cmd_result", verdict="fail", wall_s=0.0, **kw)
        self.assertEqual(L.fold(self.lg.rows()).jobs["c"]["result"]["cost"]["usd"], first)

    def test_matched_ranking_and_stall_fallback(self):
        self.post(job("cheap", budget_usd=1), "a")
        self.post(job("big", budget_usd=20), "a")
        st = L.fold(self.lg.rows())
        order, fb = B.rank(st, self.book())
        self.assertEqual((order, fb), (["cheap", "big"], {"cheap"}))            # nothing matched: cheapest, flagged
        self.bet("big", "fail", 5, "b")                                           # matched = 2 x min(5 (25% of $20), 5) = 10
        order, fb = B.rank(L.fold(self.lg.rows()), self.book())
        self.assertEqual((order, fb), (["big", "cheap"], set()))
        self.assertAlmostEqual(self.book().matched("big", st.jobs["big"]["spec"]), 10)
        self.assertEqual(B.order(st, self.lg.rows(), CFG | {"pit": {"enabled": False}})[1], set())   # old ranking when off

    def test_post_cap_per_wallet_per_hour(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = [{"t": "node", "kind": "job", "id": f"p{i}", "ts": now, "spec": {"proposer": "a.sub"}} for i in range(4)]
        cfg = {**self.PCFG, "pit": {**self.PCFG["pit"], "max_posts_per_hour": 4}}
        err = B.check_post(self.book(), cfg, job("x"), "a", rows)
        self.assertIn("posted 4 jobs in the last hour (cap 4)", err)            # subs book to their wallet
        self.assertIsNone(B.check_post(self.book(), cfg, job("x"), "b", rows))
        self.assertIsNone(B.check_post(self.book(), {**cfg, "pit": {**cfg["pit"], "max_posts_per_hour": 0}}, job("x"), "a", rows))

    def test_why_blocked_names_an_undriven_desk_job(self):
        import contextlib, io
        self.post(job("d"), "a")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.cli("cmd_why", id="d")
        self.assertIn("undriven: no run or scenario", out.getvalue())

    def test_finding_without_from_names_its_author(self):
        kw = dict(id=None, source=None, kind="finding", refutes=None, refines=None, supersedes=None)
        self.cli("cmd_finding", text="t1", agent="a", **kw)
        self.cli("cmd_finding", text="t2", agent="a", **kw)
        self.cli("cmd_finding", text="t3", agent=None, **kw)
        self.assertEqual(sorted(L.fold(self.lg.rows()).findings), ["F:a-1", "F:a-2", "F:session-1"])

    def test_rank_modes_most_uncertain_then_cheapest(self):
        flat = {**self.PCFG, "pit": {**self.PCFG["pit"], "stake_share": 0}}       # every auto stake at the floor
        self.post(job("cheap", budget_usd=1), "a", cfg=flat)
        self.post(job("big", budget_usd=20), "a", cfg=flat)
        self.bet("cheap", "fail", 0.25, "b")                                      # matched 0.5 on $1
        self.bet("big", "fail", 5, "b")                                           # matched 0.5 on $20
        st = L.fold(self.lg.rows())
        self.assertEqual(B.rank(st, self.book())[0], ["cheap", "big"])          # tie on matched: cheapest
        self.bet("big", "pass", 2, "a.sub"); self.bet("big", "fail", 5, "b")      # big: 2.25 vs 5 -> matched 4.5
        st = L.fold(self.lg.rows())
        self.assertEqual(B.rank(st, self.book())[0][0], "big")                  # most uncertain wins despite cost
        self.assertEqual(B.rank(st, self.book(), mode="matched_per_usd")[0][0], "cheap")

    def test_settlement(self):
        self.post(job("x"), "a")                                                  # a: PASS 0.25
        self.bet("x", "pass", 0.75, "a.sub")                                      # a: PASS 1.00 total
        self.bet("x", "fail", 3, "b")
        self.lg.append({"t": "result", "job": "x", "verdict": "fail", "cost": {"usd": 1, "wall_s": 1, "lane": "x"}},
                       "2026-09-29T05:00:00Z")
        s = B.settle_due(self.lg, self.PCFG)[0]
        self.assertEqual((s["pot"], s["vig"], s["payouts"]), (4.0, 0.08, {"b": 3.92, "house": 0.08}))
        self.assertEqual(B.settle_due(self.lg, self.PCFG), [])                 # never twice
        self.assertAlmostEqual(self.book().balance("b"), 53.5 + 0.92)
        # invalid: refund, no vig; cancelled: refund
        for jid, end in (("inv", {"t": "result", "job": "inv", "verdict": "invalid", "cost": {"usd": 0, "wall_s": 1, "lane": "x"}}),
                         ("gone", {"t": "cancel", "id": "gone", "reason": "dead end"})):
            self.post(job(jid), "a")
            self.bet(jid, "fail", 2, "b")
            self.lg.append(end, "2026-09-29T05:10:00Z")
            s = B.settle_due(self.lg, self.PCFG)[0]
            self.assertEqual((s["outcome"], s["vig"], s["payouts"]), ("void", 0.0, {"a": 0.25, "b": 2.0}))

    def test_corrected_verdict_resettles(self):
        def market(jid):
            self.post(job(jid), "a")                                              # a: PASS 0.25
            self.bet(jid, "pass", 0.75, "a")
            self.bet(jid, "fail", 3, "b")                                         # pot 4, vig 0.08
        market("x")
        self.settle("x", "pass")
        want = self.book().balances()                                             # pass-only settlement
        self.setUp()
        market("x")
        self.settle("x", "fail")
        s = self.settle("x", "pass", "2026-09-29T05:01:00Z")                      # (a) corrected: re-settle
        self.assertEqual(len(s), 1)
        self.assertEqual((s[0]["supersedes"], s[0]["reason"]), ("2026-09-29T05:00:00Z", "verdict corrected fail -> pass"))
        self.assertEqual(s[0]["clawback"], {"b": 3.92})
        self.assertEqual(self.book().balances(), want)                            # house keeps one vig
        self.assertEqual(self.book().balances()["house"], 0.08)
        self.assertEqual(B.settle_due(self.lg, self.PCFG), [])                  # (c) idempotent
        self.assertEqual(self.settle("x", "pass", "2026-09-29T05:02:00Z"), [])    # (b) same verdict: nothing
        self.settle("x", "fail", "2026-09-29T05:03:00Z")                          # flip back: chain stays exact
        self.settle("x", "pass", "2026-09-29T05:04:00Z")
        self.assertEqual(self.book().balances(), want)

    def test_thread_and_calibration(self):
        self.post(job("x", produces_if_fail=["F:x"]), "a")
        self.post(job("open", budget_usd=2), "b")
        self.bet("x", "fail", 1, "b")
        rows = self.lg.rows()
        L.settle(self.lg, L.fold(rows).jobs["x"]["spec"], "fail", "2026-09-29T05:00:00Z")
        self.lg.append({"t": "result", "job": "x", "verdict": "fail", "cost": {"usd": 1, "wall_s": 1, "lane": "x"}},
                       "2026-09-29T05:00:00Z")
        B.settle_due(self.lg, self.PCFG)
        out = B.thread(self.lg.rows(), "a")
        self.assertLessEqual(len(out.splitlines()), 60)
        self.assertIn("brief: a brief", out)
        self.assertIn("x [done fail]", out)
        self.assertIn("-> F:x", out)
        self.assertIn("open/main PASS $0.50 / FAIL $0.00", out)                  # b's market, a has not bet
        cal = {l.split()[0]: l.split() for l in B.calibration(self.lg.rows()).splitlines()[1:]}
        self.assertEqual(cal["b"][:3], ["b", "1", "1"])                           # 1 bet, 1 win
        self.assertEqual(cal["b"][5], f"{(1 / 1.25 - 1) ** 2:.3f}")              # implied 80% FAIL at close
        self.assertEqual(cal["b"][6], "0%")                                       # bet against the (auto) PASS side
        self.assertEqual((cal["a"][1], cal["a"][-1]), ("0", "1"))                 # a's auto stake is a self bet

    def test_board_prices_and_order(self):
        self.post(job("old", budget_usd=2), "a")                                 # PASS 0.50 (25% of $2) / FAIL 0: unopposed
        self.post(job("both"), "b")
        self.bet("both", "fail", 1, "a")                                          # opposed, matched 0.50
        self.post(job("new", budget_usd=3), "b", ts="2026-09-29T04:33:00Z")   # newest unopposed
        lines = B.board(self.lg.rows(), self.PCFG).splitlines()
        self.assertEqual([l.split()[0] for l in lines], ["new", "old", "both"])
        # $1 on the empty FAIL side: (0.50 + 1) * 0.98 / 1 = 1.47
        self.assertEqual(lines[1], "old [gpu-small, $2] PASS $0.50 / FAIL $0.00 · FAIL pays 1.5:1 · proposer a "
                                   "(0-0 on posts, 0-0 on bets)")
        self.assertIn("PASS $0.75 / FAIL $0.00 · FAIL pays 1.7:1", lines[0])
        self.assertEqual(B.pays(self.book(), "both", "main", "pass", self.PCFG), ("pass", (1.25 + 1) * 0.98 / 1.25))

    # ---- funding: budget_usd is the only budget; time and tokens burn it; the market pool is separate ----
    def rec(self, jid, verdict="pass", ts="2026-09-29T05:00:00Z", **rep):
        s = L.fold(self.lg.rows()).jobs[jid]["spec"]
        return runmod.record(self.lg, self.PCFG, s, s["lane"], {"report": {"verdict": verdict, **rep}, "wall_s": rep.get("wall_s", 0), "rc": 0}, ts)

    def test_funded_seconds(self):
        f = lambda **kw: specmod.funded_seconds(job("a", **kw), CFG["lanes"])
        self.assertEqual((f(budget_usd=1), f(budget_usd=5, lane="gpu-large"), f(budget_usd=5, lane="ci"), f(lane="any")), (257, 180, 180, 3600))
        self.assertEqual((f(budget_usd=0.01), f(budget_usd=200, lane="ci")), (30, 3600))      # floor 30 s, cap an hour
        self.assertEqual(specmod.validate(job("a", lane="any", budget_usd=0.01), CFG["lanes"]), [])   # `any` is unpriced: the hour

    def test_tokens_over_budget_fail_and_overage_charged(self):
        self.post(job("x", budget_usd=1), "a")                                   # a: -1 funding, -0.25 PASS stake
        row = self.rec("x", wall_s=60, meters={"tok_out": 400000})              # time $0.2333 + meters $0.88 = $1.1133
        self.assertEqual(row["verdict"], "fail")
        self.assertEqual(row["note"], "over budget: $1.11 of $1.00 (time $0.23, meters $0.88); trace kept")
        self.assertEqual(row["funding"], {"wallet": "a", "usd": -0.1133})
        self.assertAlmostEqual(self.book().balance("a"), 53.5 - 1 - 0.25 - 0.1133)
        (st,) = [r for r in self.lg.rows() if r["t"] == "settle"]
        self.assertEqual((st["pot"], st["payouts"]), (0.25, {"house": 0.25}))  # the pot is the stake alone: no funding in it

    def test_overage_takes_what_the_wallet_has(self):
        self.post(job("x", budget_usd=1), "b")
        self.post(job("y"), "a")
        self.bet("y", "fail", 52.2, "b")                                          # b: 53.5 - 1 - 0.25 - 52.2 = 0.05 left
        row = self.rec("x", wall_s=60, meters={"tok_out": 500000})                              # $1.3333: overage 0.3333
        self.assertEqual(row["funding"], {"wallet": "b", "usd": -0.05, "shortfall": 0.2833})
        self.assertIn("b short $0.28 of the overage", row["note"])
        self.assertAlmostEqual(self.book().balance("b"), 0.0)

    def test_under_budget_refunds_unspent_once(self):
        self.post(job("x", budget_usd=1), "a")
        row = self.rec("x", wall_s=30)                                           # $0.1167 of $1
        self.assertEqual((row["verdict"], row["funding"]), ("pass", {"wallet": "a", "usd": 0.8833}))
        paid = lambda: sum(u for r in self.lg.rows() if r["t"] == "settle" for w, u in r["payouts"].items() if w == "a") - \
            sum(u for r in self.lg.rows() if r["t"] == "settle" for w, u in r.get("clawback", {}).items() if w == "a")
        self.assertAlmostEqual(self.book().balance("a") - paid(), 53.5 - 1 - 0.25 + 0.8833)
        self.rec("x", "fail", ts="2026-09-29T05:05:00Z", wall_s=30)             # a corrected result replaces the refund, never adds a second
        self.assertEqual(list(self.book().funds.values()), [("a", 0.8833)])
        self.assertAlmostEqual(self.book().balance("a") - paid(), 53.5 - 1 - 0.25 + 0.8833)

    def test_record(self):
        self.post(job("x"), "a")                                                  # a posts PASS (self)
        self.bet("x", "pass", 1, "a.sub")                                         # self: not on a's bet record
        self.bet("x", "fail", 1, "b")
        self.settle("x", "fail")
        self.post(job("y"), "b")
        self.bet("y", "pass", 1, "a")
        self.settle("y", "pass", "2026-09-29T05:10:00Z")
        recs = B.records(self.book())
        self.assertEqual(B.record(recs, "a"), "0-1 on posts, 1-0 on bets")
        self.assertEqual(B.record(recs, "b"), "1-0 on posts, 1-0 on bets")
        self.assertIn("a (0-1 on posts, 1-0 on bets; persistent)", B.thread(self.lg.rows(), "a"))

    def test_replay_with_pit(self):
        jobs, nodes = replay.load(EXAMPLE)
        t0, sim, lg = replay.simulate(jobs, nodes, self.PCFG)
        self.assertTrue(sim["sweep-large"]["stopped"])
        for r in self.lg.rows():                                                  # the agents and their income
            lg.append(r, "2026-09-29T04:30:00Z")
        self.lg = lg
        # a $28 control on the large box: one wallet agreed, nobody took the other side
        self.post(job("control", lane="gpu-large", budget_usd=28), "a")
        self.bet("control", "pass", 1, "b")
        self.post(job("read", lane="gpu-large", budget_usd=1), "b")
        order, fb = B.order(L.fold(lg.rows()), lg.rows(), self.PCFG)
        self.assertLess(order.index("read"), order.index("control"))
        self.assertEqual(fb, {"read"})


class ListScenarios(unittest.TestCase):
    def test_list_scenarios_prints_one_per_line(self):
        import contextlib, io
        from argparse import Namespace as N
        from pit import cli
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root)
        shutil.copy(ROOT / "lanes.example.toml", root / "lanes.toml")
        (root / "ledger").mkdir()
        (root / "scenarios" / "b_scn").mkdir(parents=True)          # a scenario is a file or a directory, named by its stem
        (root / "scenarios" / "a_scn.toml").write_text("")
        (root / "scenarios" / ".hidden").write_text("")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"PIT_ROOT": str(root)}), contextlib.redirect_stdout(out):
            cli.cmd_list(N(scenarios=True, frontier=False, lane=None))
        self.assertEqual(out.getvalue(), "a_scn\nb_scn\n")
        self.assertIsNone(specmod.scenarios(root, {"bench": {"scenario_dir": "elsewhere"}}))     # no directory: accept any
        (root / "elsewhere").mkdir()
        (root / "elsewhere" / "c_scn.sh").write_text("")
        self.assertEqual(specmod.scenarios(root, {"bench": {"scenario_dir": "elsewhere"}}), ["c_scn"])


class TypicalCost(unittest.TestCase):
    """What a scenario's run costs on a lane, from its settled results: shown where an agent decides funding."""
    def setUp(self):
        from pit import cli
        self.cli, self.root = cli, Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        (self.root / "lanes.toml").write_text((ROOT / "lanes.example.toml").read_text().replace(
            "[lanes.gpu-small]\n", '[lanes.gpu-small]\nrunner = "bench --scenario {scenario} --timeout {funded_s}"\n', 1))
        (self.root / "ledger").mkdir()
        (self.root / "scenarios").mkdir()
        for n in ("s1", "s2"):
            (self.root / "scenarios" / f"{n}.toml").write_text("")
        self.lg = L.Ledger(self.root / "ledger", "h")
        runs = [("a", "s1", "pass", 100, 0.40, ""), ("b", "s1", "fail", 300, 1.20, ""),
                ("c", "s1", "fail", 257, 1.00, "over budget: killed at 257s: what the funding buys; partial trace kept"),
                ("d", "s1", "invalid", 5, 0.02, "exit 1 with no verdict")]            # invalid: not a cost sample
        for jid, sc, v, wall, usd, note in runs:
            add(self.lg, job(jid, scenario=sc))
            self.lg.append({"t": "result", "job": jid, "verdict": v, "note": note,
                            "cost": {"wall_s": wall, "usd": usd, "lane": "gpu-small"}})
        add(self.lg, job("e", run="bench --scenario 's1' --timeout 9"))              # no scenario field: read off the runner call
        self.lg.append({"t": "result", "job": "e", "verdict": "pass", "note": "", "cost": {"wall_s": 200, "usd": 0.8, "lane": "gpu-small"}})
        add(self.lg, job("f", scenario="s2"))                                          # every run killed: no typical cost
        self.lg.append({"t": "result", "job": "f", "verdict": "fail", "note": "over budget: killed at 30s",
                        "cost": {"wall_s": 30, "usd": 0.12, "lane": "gpu-small"}})
        add(self.lg, job("s2open", scenario="s2", budget_usd=0.12))
        self.cfg = lanes.load(self.root)

    def q(self, fn, **kw):
        from argparse import Namespace as N
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"PIT_ROOT": str(self.root), "PIT_HOST": "h"}), contextlib.redirect_stdout(out):
            fn(N(**kw))
        return out.getvalue()

    def test_medians_and_kill_share(self):
        t = B.typical_cost(self.lg.rows(), "s1", "gpu-small", self.cfg)
        self.assertEqual(t, {"n": 4, "wall_s": 200, "usd": 0.8, "kill": 0.25})          # medians skip the killed run c
        self.assertEqual(B.typical_cost(self.lg.rows(), "s1", "gpu-small")["n"], 3)        # no cfg: no runner template to read
        self.assertEqual(B.typical_cost(self.lg.rows(), "s2", "gpu-small", self.cfg), {"n": 1, "wall_s": None, "usd": None, "kill": 1.0})
        self.assertIsNone(B.typical_cost(self.lg.rows(), "s2", "ci", self.cfg))

    def test_scenarios_listing_shows_typical_per_lane(self):
        out = self.q(self.cli.cmd_list, scenarios=True, frontier=False, lane=None)
        self.assertEqual(out, "s1 · gpu-small typical $0.80 (200s, n=4)\ns2 · gpu-small typical unknown (all 1 runs killed)\n")

    def test_post_warns_below_typical_not_above(self):
        for jid, usd in (("low", 0.5), ("ok", 1.4)):
            p = self.root / f"{jid}.toml"
            p.write_text(f'id = "{jid}"\nquestion = "q?"\nif_pass = "go"\nif_fail = "stop"\nlane = "gpu-small"\n'
                         f'scenario = "s1"\nbudget_usd = {usd}\n')
            out = self.q(self.cli.cmd_post, spec=str(p), agent="human", seed=False, stake=0.0)
            self.assertEqual("warning: typical cost on gpu-small is $0.80 (200s); $0.5 buys 128s and will likely be killed" in out,
                             jid == "low", out)
        rows = self.lg.rows()
        board = B.board(rows, self.cfg, hide=True)
        self.assertIn("typical here $0.80 (200s)", board)
        self.assertNotIn("typical here", next(l for l in board.splitlines() if l.startswith("s2open ")))    # all killed: nothing to show
        self.assertIn("funded $1.4 (360s) · typical here $0.80 (200s)", B.new_markets(rows, self.cfg, "someone", 0))


class RefRuns(unittest.TestCase):
    def setUp(self):
        from pit import cli, trees
        self.cli, self.trees = cli, trees
        self.tmp = tempfile.TemporaryDirectory(prefix='pit-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repo = self.root / 'source with spaces'
        self.repo.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.com')
        (self.repo / 'value.txt').write_text('before\n')
        (self.repo / 'harness').mkdir()
        (self.repo / 'harness/grade.py').write_text('protected\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.git('switch', '-qc', 'feature')
        (self.repo / 'value.txt').write_text('after\n')
        (self.repo / 'scenarios').mkdir()
        (self.repo / 'scenarios/new.txt').write_text('new case\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'change')
        self.sha = self.git('rev-parse', 'HEAD').strip()
        self.lane = {'repo': str(self.repo), 'base': 'main', 'deny_paths': ['harness/**'],
                     'usd_per_h': 14, 'gate': 'true',
                     'runner': 'git -C {tree} rev-parse HEAD; pwd; echo "$PIT_TREE $PIT_REF"; cat value.txt; echo "pit: verdict=pass"'}
        self.cfg = {'lanes': {'gpu-small': self.lane}, 'pit': {'enabled': True}}
        self.lg = L.Ledger(self.root / 'ledger', 't')

    def git(self, *args):
        return self.trees.git(self.repo, *args)

    def post(self, jid='change', ref='feature', **kw):
        s = job(jid, **{'scenario': 'new', **kw})
        path = self.root / f'{jid}.toml'
        path.write_text('\n'.join(f'{k} = {json.dumps(v)}' for k, v in s.items()))
        out = io.StringIO()
        with mock.patch.object(self.cli, 'ctx', return_value=(self.root, self.lg, self.cfg)), \
             mock.patch.object(self.cli, 'sync'), contextlib.redirect_stdout(out):
            self.cli.main(['post', str(path), '--as', 'human', *([f'--ref={ref}'] if ref is not None else [])])
        return L.fold(self.lg.rows()).jobs[jid]['spec']

    def run_job(self, jid='change'):
        return runmod.run_job(self.root, self.lg, self.cfg, jid, echo=lambda *_: None)

    def listing(self, *args):
        out = io.StringIO()
        with mock.patch.object(self.cli, 'ctx', return_value=(self.root, self.lg, self.cfg)), contextlib.redirect_stdout(out):
            self.cli.main(['list', *args])
        return out.getvalue()

    def test_prepare_runs_in_the_tree_before_the_job_and_a_failing_prepare_is_invalid(self):
        self.lane['prepare'] = 'echo "$PIT_REF" > prepared.txt'
        self.lane['runner'] = 'cat prepared.txt; echo "pit: verdict=pass"'
        self.post()
        r = self.run_job()
        self.assertEqual(r['verdict'], 'pass')
        self.assertIn(self.sha, (self.root / r['log']).read_text())
        self.assertEqual(self.git('worktree', 'list', '--porcelain').count('worktree '), 1)
        self.lane['prepare'] = 'echo "inputs missing: skills"; exit 3'
        self.post('change2')
        r = self.run_job('change2')
        self.assertEqual((r['verdict'], r['note']), ('invalid', 'prepare: inputs missing: skills'))
        self.assertEqual(self.git('worktree', 'list', '--porcelain').count('worktree '), 1)

    def test_post_pins_sha_and_name_and_runner_runs_at_ref_then_removes_tree(self):
        s = self.post()
        self.assertEqual((s['ref'], s['ref_name'], s['base_ref']), (self.sha, 'feature', self.base))
        # Advancing the branch after posting cannot change the run or its diffstat.
        (self.repo / 'value.txt').write_text('later\n')
        self.git('commit', '-qam', 'later')
        r = self.run_job()
        log = (self.root / r['log']).read_text().splitlines()
        self.assertEqual(log[0], self.sha)
        self.assertEqual(log[2], f'{log[1]} {self.sha}')
        self.assertEqual(log[3], 'after')
        self.assertFalse(Path(log[1]).exists())
        self.assertEqual(self.git('worktree', 'list', '--porcelain').count('worktree '), 1)
        self.assertEqual((r['verdict'], r['ref'], r['base_ref']), ('pass', self.sha, self.base))
        self.assertIn('value.txt', r['change'])
        self.assertIn('files changed', r['change'])
        self.assertIn(self.sha, self.listing())

    def test_no_ref_runs_base_and_default_base_is_head(self):
        self.post(ref=None, scenario='', run='git rev-parse HEAD; echo "pit: verdict=pass"')
        r = self.run_job()
        self.assertEqual((r['ref'], r['change']), (self.base, ''))
        self.assertEqual(self.listing('--changes'), '')
        del self.lane['base']
        self.post('head', ref=None, scenario='', run='echo "pit: verdict=pass"')
        self.assertEqual(self.run_job('head')['ref'], self.sha)

    def test_freeform_tree_substitution_cwd_env_and_tilde_repo(self):
        with mock.patch.dict(os.environ, {'HOME': str(self.root)}):
            self.lane['repo'] = '~/source with spaces'
            self.post(scenario='', run='test "$PWD" = "$PIT_TREE" && git -C {tree} rev-parse HEAD; echo "pit: verdict=pass"', cwd='/does-not-exist')
            r = self.run_job()
        self.assertEqual((self.root / r['log']).read_text().splitlines()[0], self.sha)

    def test_bad_refs_and_refs_without_repo_leave_no_rows(self):
        for ref in ('no-such-ref', '--help', 'HEAD:value.txt'):
            with self.assertRaisesRegex(SystemExit, 'refused'):
                self.post(ref=ref)
        self.assertEqual(self.lg.rows(), [])
        del self.lane['repo']
        with self.assertRaisesRegex(SystemExit, 'ref needs a lane with repo'):
            self.post()
        self.assertEqual(self.lg.rows(), [])
        self.lane['repo'] = str(self.repo)
        with self.assertRaisesRegex(specmod.SpecError, 'ref must be a nonempty string'):
            specmod.pin_ref(job('x', ref=42), self.cfg['lanes'])

    def test_deny_paths_checks_nested_paths_and_renames(self):
        (self.repo / 'harness/deep').mkdir()
        (self.repo / 'harness/deep/card.txt').write_text('protected too')
        self.git('add', '.')
        self.git('commit', '-qm', 'nested')
        with self.assertRaisesRegex(SystemExit, 'denied path: harness/deep/card.txt'):
            self.post()
        self.git('reset', '--hard', self.sha)
        self.git('mv', 'harness/grade.py', 'grade.py')
        self.git('commit', '-qm', 'move grader')
        with self.assertRaisesRegex(SystemExit, 'denied path: harness/grade.py'):
            self.post()
        self.assertEqual(self.lg.rows(), [])

    def test_timeout_and_stop_rule_remove_tree(self):
        for jid, command, patch in [('timeout', 'pwd; sleep 30', mock.patch.object(specmod, 'funded_seconds', return_value=0.2)),
                                    ('stop', 'pwd; echo feedback_report; sleep 30', contextlib.nullcontext())]:
            self.post(jid, scenario='', run=command)
            with patch:
                r = self.run_job(jid)
            self.assertEqual(r['verdict'], 'fail')
            self.assertFalse(Path((self.root / r['log']).read_text().splitlines()[0]).exists())
        self.assertEqual(self.git('worktree', 'list', '--porcelain').count('worktree '), 1)

    def test_sigterm_removes_tree_and_kills_child(self):
        import time
        marker = self.root / 'started'
        code = '''import sys
from pathlib import Path
from pit import trees, run
with trees.worktree(sys.argv[1], 'feature') as tree:
    run.execute('pwd > "$MARKER"; sleep 30', 30, [], cwd=tree, env={'MARKER': sys.argv[2]})
'''
        p = subprocess.Popen([sys.executable, '-c', code, str(self.repo), str(marker)])
        self.addCleanup(lambda: p.poll() is None and (p.kill(), p.wait()))
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(marker.exists())
        tree = Path(marker.read_text().strip())
        p.terminate()
        p.wait(timeout=5)
        self.assertFalse(tree.exists())
        self.assertEqual(self.git('worktree', 'list', '--porcelain').count('worktree '), 1)

    def test_list_changes_newest_first_skips_fail_baseline_and_corrections(self):
        for jid, ref, verdict in [('older', 'feature', 'pass'), ('base', 'main', 'pass'),
                                  ('failed', 'feature', 'fail'), ('newer', 'feature', 'pass')]:
            self.post(jid, ref=ref, scenario='', run=f'echo "pit: verdict={verdict}"')
            self.run_job(jid)
        out = self.listing('--changes')
        self.assertLess(out.index('newer PASS'), out.index('older PASS'))
        self.assertNotIn('failed', out)
        self.assertNotIn('base PASS', out)
        self.assertIn('feature', out)
        self.assertIn('value.txt', out)
        latest = L.fold(self.lg.rows()).jobs['newer']['result']
        self.lg.append({**latest, 'verdict': 'fail'})
        self.assertNotIn('newer PASS', self.listing('--changes'))

    def test_blind_views_mark_change_market_json_has_ref_name(self):
        self.lg.append(B.agent_row(B.Book([]), 'a', 'a brief'))
        s = specmod.pin_ref(job('change', ref='feature', proposer='a'), self.cfg['lanes'])
        add(self.lg, s, ts=L.now())
        rows = self.lg.rows()
        for out in (B.board(rows, self.cfg, hide=True), B.new_markets(rows, self.cfg, 'human', 0),
                    B.digest(rows, [1], self.cfg), B.thread(rows, 'a', hide=True)):
            self.assertIn('◇', out)
            self.assertNotIn('value.txt', out)
            self.assertNotIn('PASS $', out)
        j = market.market_json(rows, self.cfg)['markets'][0]
        self.assertEqual((j['ref'], j['ref_name'], j['has_change']), (self.sha, 'feature', True))
        self.assertNotIn('change', j)

    def test_registry_command_reads_posted_tree(self):
        self.cfg['bench'] = {'scenario_cmd': 'test "$PIT_REF" = "$(git -C {tree} rev-parse HEAD)" && test -f scenarios/new.txt && echo \'["new"]\''}
        self.post()
        self.assertEqual(self.git('worktree', 'list', '--porcelain').count('worktree '), 1)

    def test_remote_url_ref_is_also_pinned(self):
        self.lane.update(url='http://runner', repo=self.repo.as_uri())
        s = self.post(scenario='', run='echo "pit: verdict=pass"')
        self.assertEqual((s['ref'], s['ref_name']), (self.sha, 'feature'))

    def test_result_correction_preserves_proof_and_thread_has_diffstat(self):
        self.lg.append(B.agent_row(B.Book([]), 'a', 'capability'))
        s = specmod.pin_ref(job('change', proposer='a', ref='feature', run='echo "pit: verdict=fail"'), self.cfg['lanes'])
        add(self.lg, s, ts=L.now())
        self.run_job()
        with mock.patch.object(self.cli, 'ctx', return_value=(self.root, self.lg, self.cfg)), \
             mock.patch.object(self.cli, 'sync'), contextlib.redirect_stdout(io.StringIO()):
            self.cli.main(['result', 'change', '--verdict', 'pass'])
        self.assertIn('change PASS', self.listing('--changes'))
        thread = B.thread(self.lg.rows(), 'a', hide=True)
        self.assertIn('value.txt', thread)
        self.assertIn(self.sha, thread)
        self.lg.append(B.agent_row(B.Book(self.lg.rows()), 'b', 'other'))
        self.lg.append({'t': 'claim', 'job': 'change', 'lane': 'gpu-small', 'cid': 'c-b', 'agent': 'b'})
        self.assertIn('value.txt', B.thread(self.lg.rows(), 'b', hide=True))
        self.assertNotIn('change', market.market_json(self.lg.rows(), self.cfg)['threads']['a']['nodes'][0])

    def test_diff_prints_stat_then_patch_and_refuses_without_change(self):
        self.post()
        out = io.StringIO()
        with mock.patch.object(self.cli, 'ctx', return_value=(self.root, self.lg, self.cfg)), contextlib.redirect_stdout(out):
            self.cli.main(['diff', 'change'])
        text = out.getvalue()
        self.assertLess(text.index('files changed'), text.index('-before'))
        self.assertIn('+after', text)
        self.post('baseline', ref='main', scenario='', run='echo "pit: verdict=pass"')
        with mock.patch.object(self.cli, 'ctx', return_value=(self.root, self.lg, self.cfg)), \
             self.assertRaisesRegex(SystemExit, 'baseline carries no change'):
            self.cli.main(['diff', 'baseline'])

    def test_checkout_error_is_invalid_and_lane_without_repo_keeps_cwd(self):
        self.post(scenario='', run='echo "pit: verdict=pass"')
        with mock.patch.object(self.trees, 'worktree', side_effect=self.trees.GitError('checkout failed')):
            r = self.run_job()
        self.assertEqual(r['verdict'], 'invalid')
        self.assertIn('checkout failed', r['note'])
        del self.lane['repo']
        self.post('old-cwd', ref=None, scenario='', run='pwd; echo "pit: verdict=pass"', cwd=str(self.repo))
        r = self.run_job('old-cwd')
        self.assertEqual(Path((self.root / r['log']).read_text().splitlines()[0]), self.repo.resolve())
        self.assertNotIn('ref', r)


if __name__ == "__main__":
    unittest.main()
