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
from pit import hooks, view, reflect, lanes, ledger as L, book as B, replay, run as runmod, spec as specmod

ROOT = Path(__file__).resolve().parent.parent
EXAMPLE = ROOT / "examples" / "replay-synthetic"
CFG = tomllib.loads((ROOT / "lanes.example.toml").read_text())


def job(id, **kw):
    s = {"id": id, "question": f"does {id} hold?", "expect": "pass", "if_pass": "go", "if_fail": "stop",
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
        self.assertTrue(any("expect" in e for e in self.errs(expect="maybe")))
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
        self.assertEqual((runmod.verdict_of(r)[0], r["report"]["meters"], r["report"]["result"]), ("fail", {"out_lines": 2, "out_bytes": 3}, {"rc": 1}))

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


if __name__ == "__main__":
    unittest.main()


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
                            parent=None, reason=None, by="reflect"))
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
        self.assertAlmostEqual(b.balance("a"), a_before - 3.25)                 # its wallet pays, as before
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

    def bet(self, job, side, usd, agent, variant="main", ts="2026-09-29T04:32:00Z"):
        rows = self.lg.rows()
        return self.lg.append(B.bet_row(B.Book(rows), L.fold(rows), job, variant, side, usd, agent), ts)

    def test_drip_split_and_idempotent(self):
        d = [r for r in self.lg.rows() if r["t"] == "drip"][0]
        self.assertEqual(d["minutes"], 30)
        self.assertEqual(d["to"], {"a": 53.5, "b": 53.5})                        # subs get nothing
        self.assertIsNone(B.tick(self.lg, self.PCFG, self.T0 + timedelta(seconds=59)))
        r = B.tick(self.lg, self.PCFG, self.T0 + timedelta(seconds=150))
        self.assertEqual((r["minutes"], r["until"]), (2, "2026-09-29T04:32:00.000Z"))   # leftover seconds carry over
        self.assertAlmostEqual(self.book().balance("a"), 53.5 + 214 / 60, places=3)

    def test_post_debits_and_auto_stakes(self):
        self.post(job("x", budget_usd=10, expect="fail"), "a.sub")               # a sub spends from its parent
        b = self.book()
        self.assertAlmostEqual(b.balance("a"), 53.5 - 10 - 0.25)
        self.assertEqual(b.bets[0]["tags"], ["auto", "self"])
        self.assertEqual((b.bets[0]["side"], b.bets[0]["book"]), ("fail", "a"))
        self.assertIn("needs", B.check_post(b, self.PCFG, job("y", budget_usd=500, lane="ci"), "b"))

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
        self.bet("big", "fail", 5, "b")                                           # matched = 2 x min(0.25, 5) = 0.5
        order, fb = B.rank(L.fold(self.lg.rows()), self.book())
        self.assertEqual((order, fb), (["big", "cheap"], set()))
        self.assertAlmostEqual(self.book().matched("big", st.jobs["big"]["spec"]), 0.5)
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
        self.post(job("cheap", budget_usd=1), "a")
        self.post(job("big", budget_usd=20), "a")
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
        self.assertIn("open/main PASS $0.25 / FAIL $0.00", out)                  # b's market, a has not bet
        cal = {l.split()[0]: l.split() for l in B.calibration(self.lg.rows()).splitlines()[1:]}
        self.assertEqual(cal["b"][:3], ["b", "1", "1"])                           # 1 bet, 1 win
        self.assertEqual(cal["b"][5], f"{(1 / 1.25 - 1) ** 2:.3f}")              # implied 80% FAIL at close
        self.assertEqual(cal["b"][6], "0%")                                       # bet against the (auto) PASS side
        self.assertEqual((cal["a"][1], cal["a"][-1]), ("0", "1"))                 # a's auto stake is a self bet

    def test_board_prices_and_order(self):
        self.post(job("old", budget_usd=2), "a")                                 # PASS 0.25 / FAIL 0: unopposed
        self.post(job("both"), "b")
        self.bet("both", "fail", 1, "a")                                          # opposed, matched 0.50
        self.post(job("new", budget_usd=3, expect="fail"), "b", ts="2026-09-29T04:33:00Z")   # newest unopposed
        lines = B.board(self.lg.rows(), self.PCFG).splitlines()
        self.assertEqual([l.split()[0] for l in lines], ["new", "old", "both"])
        # $1 on the empty FAIL side: (0.25 + 1) * 0.98 / 1 = 1.225
        self.assertEqual(lines[1], "old [gpu-small, $2] PASS $0.25 / FAIL $0.00 · FAIL pays 1.2:1 · proposer a "
                                   "(0-0 on posts, 0-0 on bets)")
        self.assertIn("PASS pays 1.2:1", lines[0])
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
        # a $28 control on the large box: expect fail, one wallet agreed, nobody took the other side
        self.post(job("control", lane="gpu-large", budget_usd=28, expect="fail"), "a")
        self.bet("control", "fail", 1, "b")
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
