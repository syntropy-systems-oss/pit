import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

from pit import ledger as L, run as runmod, runner_service as R
from tests.test_pit import CFG, add, job

QUIET = dict(echo=lambda *_: None)


class Runner(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), R.make_handler(self.tmp / "work", 1, None))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        self.cfg = {**CFG, "lanes": {**CFG["lanes"], "gpu-small": {**CFG["lanes"]["gpu-small"], "url": self.url}}}

    def test_job_on_a_url_lane_runs_on_the_runner(self):
        (self.tmp / "ledger").mkdir()
        lg = L.Ledger(self.tmp / "ledger", "t")
        add(lg, job("r", budget_usd=1, run="""echo "job $PIT_JOB for $PIT_FUNDED_S s in $(basename "$PWD")"; echo "claim: $PIT_CLAIM"; """
                                            """echo 'pit: verdict=pass meters={"tok_out": 100000, "widgets": 3} result={"k": 1}'"""))
        out = []
        row = runmod.run_job(self.tmp, lg, self.cfg, "r", echo=out.append)
        self.assertIn("  | job r for 257 s in slot-0\n", [o + "\n" for o in out])      # env and the runner's own working dir
        self.assertIn("  | claim: r holds", out)                                     # a driver may judge the trace against it
        self.assertEqual((row["verdict"], row["result"]), ("pass", {"k": 1}))
        c = row["cost"]
        self.assertLess(c["wall_s"], 5)                                               # the runner's wall_s line, not the client's
        self.assertAlmostEqual(c["usd"], round(c["wall_s"] / 3600 * 14 + 0.22, 4), places=3)   # wall x $14/h + 0.1M tok_out x $2.20
        self.assertEqual((c["meters"]["widgets"], c["unpriced"]), (3, ["widgets"]))

    def test_the_runner_kills_at_funded_s_and_reports_the_whole_wall(self):
        t0 = time.monotonic()
        r = runmod.execute_remote(self.url, {"job": "k", "command": "echo start; sleep 30; echo never", "funded_s": 1, "env": {}}, [], **QUIET)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual((r["stop"], runmod.verdict_of(r)[0]), ("timeout", "fail"))
        self.assertNotIn("never", r["output"])
        self.assertGreaterEqual(r["report"]["wall_s"], 1.0)

    def test_repo_ref_script_uses_a_persistent_checkout(self):
        repo = self.tmp / "src"
        git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
        repo.mkdir()
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@example.com"); git("config", "user.name", "t")
        (repo / "v.txt").write_text("one\n"); (repo / ".gitignore").write_text("*.cache\n"); git("add", "."); git("commit", "-qm", "one")
        git("checkout", "-qb", "feature"); (repo / "v.txt").write_text("two\n"); git("commit", "-qam", "two")
        body = lambda ref: {"job": "g", "repo": str(repo), "ref": ref, "script": "cat v.txt; test -e built.cache && echo reused; test -e stray.txt && echo stray; touch built.cache stray.txt; echo 'pit: verdict=pass'", "funded_s": 30, "env": {}}
        self.assertIn("one\n", runmod.execute_remote(self.url, body("main"), [], **QUIET)["output"])
        r = runmod.execute_remote(self.url, body("feature"), [], **QUIET)
        self.assertIn("two\n", r["output"])
        self.assertEqual(runmod.verdict_of(r)[0], "pass")
        self.assertIn("reused\n", r["output"])                                        # an ignored build output survives the next ref
        self.assertNotIn("stray\n", r["output"])                                      # an untracked file does not
        self.assertEqual(len(list((self.tmp / "work" / "checkouts").iterdir())), 1)  # one checkout, fetched twice
        bad = runmod.execute_remote(self.url, body("no-such-ref"), [], **QUIET)
        self.assertEqual(runmod.verdict_of(bad)[0], "invalid")

    def test_busy_and_bad_requests_are_refused(self):
        self.assertEqual(runmod.execute_remote(self.url, {"job": "x", "funded_s": 5, "env": {}}, [], **QUIET)["report"]["verdict"], "invalid")
        th = threading.Thread(target=runmod.execute_remote, args=(self.url, {"job": "a", "command": "sleep 2", "funded_s": 5, "env": {}}, []), kwargs=QUIET)
        th.start()
        time.sleep(0.5)
        r = runmod.execute_remote(self.url, {"job": "b", "command": "true", "funded_s": 5, "env": {}}, [], **QUIET)
        self.assertIn("busy", r["report"]["note"])
        th.join()


class RefProtocol(unittest.TestCase):
    """Exercise the runner's repo protocol without a listening socket."""
    def test_script_tree_env_and_metadata_reach_the_dispatcher_parser(self):
        import io
        from pit import trees
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / 'source'
            repo.mkdir()
            trees.git(repo, 'init', '-q', '-b', 'main')
            trees.git(repo, 'config', 'user.name', 'Test')
            trees.git(repo, 'config', 'user.email', 'test@example.com')
            (repo / 'value=result=').write_text('before')
            trees.git(repo, 'add', '.')
            trees.git(repo, 'commit', '-qm', 'base')
            base = trees.resolve(repo, 'HEAD')
            trees.git(repo, 'switch', '-qc', 'feature')
            (repo / 'value=result=').write_text('after')
            trees.git(repo, 'commit', '-qam', 'change')
            sha = trees.resolve(repo, 'HEAD')
            handler = R.make_handler(root / 'runner', 1, None)
            h = object.__new__(handler)
            h.send_response = h.send_header = h.end_headers = lambda *a: None
            h.wfile = io.BytesIO()
            h.run({'repo': str(repo), 'ref': sha, 'base': base,
                   'script': 'git -C {tree} rev-parse HEAD; echo "$PIT_TREE $PIT_REF"; echo "pit: verdict=pass"'},
                  10, {}, 0)
            lines = h.wfile.getvalue().decode().splitlines()
            self.assertEqual(lines[0], sha)
            self.assertTrue(lines[1].endswith(' ' + sha))
            report = runmod.parse_line(lines[-1])
            self.assertEqual(report['tree']['ref'], sha)
            self.assertEqual(report['tree']['base_ref'], base)
            self.assertIn('value', report['tree']['change'])
            self.assertIn('1 file changed', report['tree']['change'])


if __name__ == "__main__":
    unittest.main()


class Slots(unittest.TestCase):
    def test_lowest_free_slot_from_running_claims(self):
        from pit import ledger as L, run as runmod
        lg = L.MemLedger()
        for j in ("a", "b", "c"):
            lg.append({"t": "node", "kind": "job", "id": j, "spec": {"id": j, "lane": "gpu", "budget_usd": 1}})
        lg.append({"t": "claim", "job": "a", "lane": "gpu", "cid": "x:1", "slot": 0})
        st = L.fold(lg.rows())
        self.assertEqual(runmod.free_slot(st, "gpu", 2), 1)           # slot 0 is held by a
        self.assertEqual(runmod.free_slot(st, "other", 2), 0)         # another lane: nothing held
        lg.append({"t": "claim", "job": "b", "lane": "gpu", "cid": "x:2", "slot": 1})
        self.assertEqual(runmod.free_slot(L.fold(lg.rows()), "gpu", 2), 2)   # full: the next index, never a collision
        lg.append({"t": "result", "job": "a", "verdict": "pass", "cost": {"usd": 0, "lane": "gpu", "wall_s": 1}, "result": {}})
        self.assertEqual(runmod.free_slot(L.fold(lg.rows()), "gpu", 2), 0)   # a finished: slot 0 is free again
