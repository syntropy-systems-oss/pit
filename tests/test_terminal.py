import json
import threading
import unittest
import urllib.request
from datetime import datetime, timezone

from pit import lanes, ledger as L, market, book as B, view
from tests.test_pit import CFG, EXAMPLE, add, job

PCFG = {**CFG, "pit": {"enabled": True, "default_stake": 0.25, "vig_rate": 0.02}}


class Terminal(unittest.TestCase):
    def setUp(self):
        self.lg = lg = L.MemLedger()
        book = B.Book([])
        for aid in ("a", "b"):
            book.agents[aid] = lg.append(B.agent_row(book, aid, f"{aid} brief"), "2026-09-29T04:00:00Z")
        B.tick(lg, PCFG, datetime(2026, 9, 29, 4, 30, tzinfo=timezone.utc), since="2026-09-29T04:00:00Z")
        T = "2026-09-29T04:31:00Z"
        for jid, who in (("x", "a"), ("y", "b")):
            add(lg, {**job(jid, produces_if_pass=[f"F:{jid}"]), "proposer": who}, ts=T)
            for r in B.stakes(lg.rows(), PCFG, job(jid), who, "agent"):
                lg.append(r, T)
        lg.append(B.bet_row(B.Book(lg.rows()), L.fold(lg.rows()), "x", "main", "fail", 0.5, "b"), "2026-09-29T04:32:00Z")
        lg.append({"t": "claim", "job": "x", "lane": "gpu-small", "cid": "sim:1", "agent": "a"}, "2026-09-29T04:33:00Z")
        lg.append({"t": "result", "job": "x", "verdict": "pass", "cost": {"usd": 1.5, "wall_s": 9, "lane": "gpu-small"}}, "2026-09-29T04:40:00Z")
        L.settle(lg, job("x", produces_if_pass=["F:x"]), "pass", "2026-09-29T04:40:00Z")
        B.settle_due(lg, PCFG)
        lg.append({"t": "sleep", "agent": "b", "until": {"result": "y"}, "note": "waiting"}, "2026-09-29T04:41:00Z")
        self.rows, self.now = lg.rows(), datetime(2026, 9, 29, 5, tzinfo=timezone.utc)
        self.m = market.market_json(self.rows, PCFG, self.now)

    def test_shape(self):
        m = self.m
        self.assertEqual([x["task"] for x in m["markets"]], ["y"])            # x is settled and done
        y = m["markets"][0]
        self.assertEqual((y["pass"], y["fail"], y["matched"], y["proposer"], y["state"]), (0.25, 0.0, 0.0, "b", "queued"))
        self.assertTrue(y["fallback"])
        self.assertEqual(y["bets"][0]["agent"], "b")
        ag = {a["id"]: a for a in m["agents"]}
        self.assertTrue(ag["b"]["sleeping"] and not ag["a"]["sleeping"])
        book = B.Book(self.rows)
        self.assertAlmostEqual(ag["a"]["balance"], book.balance("a"), 3)
        self.assertAlmostEqual(ag["a"]["series"][-1][1], book.balance("a"), 3)        # the sparkline ends at the wallet
        self.assertAlmostEqual(ag["b"]["series"][-1][1], book.balance("b"), 3)
        lane = {l["lane"]: l for l in m["lanes"]}["gpu-small"]
        self.assertEqual((lane["depth"], lane["queue"][0]["task"], lane["queue"][0]["fallback"], lane["running"]), (1, "y", True, None))
        self.assertEqual(lane["spend_today"], 1.5)
        self.assertEqual({c["agent"] for c in m["calibration"]}, {"a", "b"})
        self.assertEqual(m["top"]["story"]["pass"], 1)
        self.assertEqual(m["top"]["story"]["findings"], 1)
        self.assertAlmostEqual(m["top"]["house"], book.flows["house"], 3)
        self.assertEqual([n["id"] for n in m["threads"]["a"]["nodes"]], ["x"])
        self.assertEqual(m["threads"]["a"]["nodes"][0]["findings"][0]["id"], "F:x")
        kinds = [e["type"] for e in m["tape"]]
        for k in ("BET", "CLAIM", "RESULT", "SETTLE", "FINDING", "SLEEP", "DRIP", "POST"):
            self.assertIn(k, kinds)
        self.assertEqual(m["tape"][0]["i"], len(self.rows) - 1)                       # newest first
        bet = next(e for e in m["tape"] if e["type"] == "BET" and e["agent"] == "b")
        self.assertIn("FAIL  $0.50  (book 0.25/0.50)", bet["text"])
        settle = next(e for e in m["tape"] if e["type"] == "SETTLE")
        self.assertIn("winners", settle["text"])
        json.dumps(m)

    def test_bet_counts_per_side(self):
        """n: bets per side, with the proposer's own stake, the house and humans kept apart from agents' bets."""
        self.assertEqual(self.m["markets"][0]["n"]["pass"], {"total": 1, "agents": 0, "self": 1, "house": 0, "human": 0})
        lg = L.MemLedger()
        for r in self.rows:
            lg.append(r, r["ts"])
        T = "2026-09-29T04:50:00Z"
        for side, usd in (("pass", 0.3), ("fail", 0.4)):
            lg.append(B.bet_row(B.Book(lg.rows()), L.fold(lg.rows()), "y", "main", side, usd, "a", why="x"), T)
        for side, who, tag in (("pass", B.HOUSE, "seed"), ("fail", B.HUMAN, "human")):
            lg.append({"t": "bet", "job": "y", "variant": "main", "side": side, "usd": 1.0, "agent": who, "book": who, "tags": [tag]}, T)
        y = market.market_json(lg.rows(), PCFG, self.now)["markets"][0]
        self.assertEqual(y["n"], {"pass": {"total": 3, "agents": 1, "self": 1, "house": 1, "human": 0},
                                  "fail": {"total": 2, "agents": 1, "self": 0, "house": 0, "human": 1}})
        page = view.TERMINAL.read_text()
        for f in ("mk.n[s]", ".agents", ".total", '"self"', '"house"', '"human"'):
            self.assertIn(f, page)

    def test_subs_are_turns(self):
        lg, book = L.MemLedger(), B.Book([])
        for aid, parent, ts in (("a", None, "04:00"), ("b", None, "04:00"), ("a-1", "a", "04:10"), ("a-2", "a", "04:20"), ("b-1", "b", "04:15")):
            book.agents[aid] = lg.append(B.agent_row(book, aid, "x", parent), f"2026-09-29T{ts}:00Z")
        m = market.market_json(lg.rows(), PCFG, self.now)
        ag = {a["id"]: (a["turns"], a["last_turn"]) for a in m["agents"]}
        self.assertEqual(ag, {"a": (2, "2026-09-29T04:20:00Z"), "b": (1, "2026-09-29T04:15:00Z")})
        self.assertEqual(m["top"]["subs"], 3)

    def test_running_and_server(self):
        rows = self.rows[:next(i for i, r in enumerate(self.rows) if r["t"] == "result")]      # up to the claim
        m = market.market_json(rows, PCFG, self.now)
        run = {l["lane"]: l for l in m["lanes"]}["gpu-small"]["running"]
        self.assertEqual((run["job"], run["proposer"], run["elapsed_s"]), ("x", "a", 27 * 60))
        self.assertEqual((run["burned_usd"], run["funded_s"]), (6.3, 257))       # 27 min x $14/h burned of a $1 funding (257 s)
        srv = view.ThreadingHTTPServer(("127.0.0.1", 0), view.make_handler(lambda: self.rows, PCFG))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{srv.server_port}"
        try:
            full = json.load(urllib.request.urlopen(base + "/market.json"))
            cut = json.load(urllib.request.urlopen(base + "/market.json?upto=5"))
            self.assertEqual((full["total_rows"], cut["rows"], len(cut["row_ts"])), (len(self.rows), 5, 5))
            self.assertIn(b"market.json", urllib.request.urlopen(base + "/terminal").read())
        finally:
            srv.shutdown()
            srv.server_close()


    def test_example_replay(self):
        """The shipped synthetic replay renders: every lane it names, both agents, its settled markets in the tape."""
        rows = L.Ledger(EXAMPLE / "ledger").rows()
        m = market.market_json(rows, CFG, B.parse_t(rows[-1]["ts"]))
        self.assertTrue({"gpu-small", "gpu-large", "ci"} <= {l["lane"] for l in m["lanes"]})
        self.assertEqual({a["id"] for a in m["agents"]}, {"explorer", "skeptic"})
        self.assertIn("SETTLE", {e["type"] for e in m["tape"]})
        json.dumps(m)


if __name__ == "__main__":
    unittest.main()
