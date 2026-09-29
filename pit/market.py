"""market_json(): what the terminal (view/terminal.html) needs, folded from the (already sliced) ledger. Read-only; reuses ledger.fold and B.Book/order/sleepers/calibration."""
from datetime import datetime, timezone

from . import ledger as L, book as B

TAPE = 200


def _calibration(rows):
    """B.calibration() renders text; read its table back (header-driven) rather than duplicate the maths."""
    head, *body = B.calibration(rows).splitlines()
    keys = head.split()
    return [dict(zip(keys, ln.split())) for ln in body]


def _walk(rows, book, cfg):
    """One pass over the rows: per-wallet balance series, and the one-line tape events."""
    flows, series, tot, closed, done, events = {}, {}, {}, set(), set(), []

    def move(w, usd, i):
        flows[w] = round(flows.get(w, 0.0) + usd, 4)
        series.setdefault(w, []).append([i, flows[w]])

    for i, r in enumerate(rows):
        t = r["t"]
        ev = {"i": i, "ts": r["ts"], "type": t.upper(), "agent": r.get("agent"), "job": r.get("job")}
        if t == "drip":
            for a, u in r["to"].items():
                move(a, u, i)
            ev.update(type="DRIP", text=f"+${r['usd']:.2f} minted, {r['minutes']} min, {len(r['to'])} agents")
        elif t == "agent":
            ev.update(type="AGENT", agent=r["id"], text=f"{r['kind']}" + (f" under {r['parent']}" if r.get("parent") else "") + f": {r['brief'][:80]}")
        elif t == "node" and r.get("kind") == "job":
            s, who = r["spec"], r["spec"].get("proposer")
            if who and not s.get("seed") and who != B.HUMAN:
                move(book.wallet(who), -s.get("budget_usd", 0), i)
            ev.update(type="POST", agent=who, job=r["id"], text=f"{r['id']}  ${s.get('budget_usd', 0):.2f} {s.get('lane', '')}  {s.get('question', '')[:80]}")
        elif t == "node":
            ev.update(type="FINDING", job=r["id"], text=f"{r['id']} ({r.get('kind')}) from {r.get('from')}: {r.get('text', '')[:100]}")
        elif t == "edge":
            ev.update(type="REFUTE" if r["type"] == "refutes" else "EDGE", job=r["from"], text=f"{r['from']} {r['type']} {r['to']}")
        elif t == "claim":
            closed.add(r["job"])
            ev.update(type="CLAIM", text=f"{r['job']} on {r.get('lane', '')}" + ("  [fallback]" if r.get("fallback") else ""))
        elif t == "bet":
            key, live = (r["job"], r["variant"]), r["job"] not in closed
            book_t = tot.setdefault(key, {"pass": 0.0, "fail": 0.0})
            if live:
                book_t[r["side"]] = round(book_t[r["side"]] + r["usd"], 4)
                move(r["book"], -r["usd"], i)
            tags = r.get("tags", [])
            ev.update(type="BET", side=r["side"].upper(), usd=r["usd"], variant=r["variant"], self="self" in tags, tags=tags,
                      text=f"{r['job']}/{r['variant']}  {r['side'].upper()}  ${r['usd']:.2f}  (book {book_t['pass']:.2f}/{book_t['fail']:.2f})"
                           + ("  [self]" if "self" in tags else "") + ("  [seed]" if "seed" in tags else "") + ("" if live else "  [late, ignored]"))
        elif t == "result":
            c = r.get("cost", {})
            ev.update(type="RESULT", verdict=r["verdict"], usd=c.get("usd", 0), text=f"{r['job']}  {r['verdict'].upper()}  ${c.get('usd', 0):.2f}  {c.get('wall_s', 0):.0f}s {c.get('lane', '')}")
        elif t == "settle":
            if (r["job"], r["variant"]) not in done:       # settle rows count once, like Book
                done.add((r["job"], r["variant"]))
                for w, u in r["payouts"].items():
                    move(w, u, i)
            win = ", ".join(f"{w} +${u:.2f}" for w, u in sorted(r["payouts"].items(), key=lambda x: -x[1]) if w != B.HOUSE) or "none"
            ev.update(type="SETTLE", variant=r["variant"], text=f"{r['job']}/{r['variant']}  {r['outcome'].upper()}  pot ${r['pot']:.2f}  winners {win}  vig ${r['vig']:.2f}")
        elif t == "cancel":
            ev.update(type="CANCEL", job=r["id"], text=f"{r['id']}: {r.get('reason', '')[:80]}")
        elif t == "reflect":
            ev.update(type="REFLECT", text=(r.get("note") or "")[:100])
        elif t == "sleep":
            ev.update(type="SLEEP", text=f"until {B.until_text(r['until'])}: {r.get('note', '')[:80]}")
        elif t == "wake":
            ev.update(type="WAKE", text=r.get("reason", ""))
        else:
            ev.update(text=" ".join(f"{k}={str(v)[:40]}" for k, v in r.items() if k not in ("t", "ts", "host")))
        events.append(ev)
    return series, events


def market_json(rows: list[dict], cfg: dict, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    st, book = L.fold(rows), B.Book(rows)
    series, events = _walk(rows, book, cfg)
    frontier = st.frontier()
    order, fb = B.order(st, rows, cfg, frontier)
    born = {r["id"]: r["ts"] for r in rows if r["t"] == "node"}
    lanes = {n: l for n, l in cfg.get("lanes", {}).items() if n != "any"}

    def market(jid):
        j = st.jobs[jid]
        s = j["spec"]
        budget = s["budget_usd"]
        m = book.matched(jid, s)
        vs = []
        for v in B.variants(s):
            t = book.totals(jid, v)
            bets = [b for b in book.bets if b["job"] == jid and b["variant"] == v]
            vs.append({"variant": v, **t, "matched": round(2 * min(t.values()), 4),
                       "pass_by": sorted({b["agent"] for b in bets if b["side"] == "pass"}),
                       "fail_by": sorted({b["agent"] for b in bets if b["side"] == "fail"}),
                       "settled": (book.settled.get((jid, v)) or {}).get("outcome")})
        return {"task": jid, "variants": vs, "pass": round(sum(v["pass"] for v in vs), 4), "fail": round(sum(v["fail"] for v in vs), 4),
                "matched": round(m, 4), "budget": budget, "score": round(m / max(budget, 0.01), 4), "lane": s["lane"],
                "proposer": book.proposers.get(jid), "fallback": jid in fb, "state": j["state"], "runnable": jid in frontier,
                "rank": order.index(jid) if jid in order else None, "added": j["added"], "age_s": _age(born.get(jid), now),
                "question": s["question"], "expect": s["expect"], "if_pass": s["if_pass"], "if_fail": s["if_fail"],
                "bets": [{"i": i, "ts": r["ts"], "agent": r["agent"], "variant": r["variant"], "side": r["side"], "usd": r["usd"], "tags": r.get("tags", [])}
                         for i, r in enumerate(rows) if r["t"] == "bet" and r["job"] == jid]}
    markets = [market(j) for j, v in st.jobs.items() if v["state"] in ("queued", "running")]
    mk = {m["task"]: m for m in markets}

    today, spend = now.date().isoformat(), {}
    for r in st.results:
        if r["ts"][:10] == today:
            k = r["cost"].get("lane", "?")
            spend[k] = round(spend.get(k, 0) + r["cost"]["usd"], 4)
    lane_out = []
    for name, l in lanes.items():
        run = next(({"job": j, "proposer": book.proposers.get(j), "since": v["claim"]["ts"], "elapsed_s": _age(v["claim"]["ts"], now),
                     "budget_s": v["spec"]["budget_s"], "budget_usd": v["spec"]["budget_usd"], "fallback": bool(v["claim"].get("fallback"))}
                    for j, v in st.jobs.items() if v["state"] == "running" and v["spec"]["lane"] == name), None)
        q = [j for j in order if st.jobs[j]["spec"]["lane"] == name]
        lane_out.append({"lane": name, "price": l.get("usd_per_h", 0), "box": l.get("box", ""), "running": run,
                         "depth": len(q), "queue": [{"task": j, "score": mk[j]["score"], "matched": mk[j]["matched"], "budget": mk[j]["budget"],
                                                     "fallback": j in fb, "proposer": mk[j]["proposer"]} for j in q], "spend_today": spend.get(name, 0.0)})

    sleepers, fams = B.sleepers(rows), {}
    agents = []
    for a, r in book.agents.items():
        z = sleepers.get(a)
        w = book.wallet(a)
        agents.append({"id": a, "kind": r["kind"], "parent": r.get("parent"), "brief": r["brief"], "wallet": w, "balance": round(book.balance(a), 4),
                       "series": series.get(w, [])[-80:], "sleeping": bool(z and not z["wake"]),
                       "sleep": None if not z else {"until": B.until_text(z["sleep"]["until"]), "note": z["sleep"].get("note", ""), "since": z["sleep"]["ts"],
                                                     "woke": z["wake"]["reason"] if z["wake"] else None}})
    claimed = {}
    for r in rows:
        if r["t"] == "claim" and r.get("agent"):
            claimed.setdefault(r["agent"], set()).add(r["job"])
    threads = {}
    for a in book.agents:
        fam = book.family(a)
        mine = {j for j in st.jobs if book.proposers.get(j) in fam} | {j for f in fam for j in claimed.get(f, ())}
        nodes = []
        for jid in sorted(mine, key=lambda j: st.jobs[j]["added"]):
            j = st.jobs[jid]
            res = j["result"]
            nodes.append({"id": jid, "state": j["state"], "verdict": res["verdict"] if res else None, "usd": (res or {}).get("cost", {}).get("usd", 0),
                          "budget": j["spec"]["budget_usd"], "question": j["spec"]["question"], "by": book.proposers.get(jid),
                          "findings": [{"id": f, "kind": x["kind"], "status": x["status"], "text": x["text"]} for f, x in st.findings.items() if x["from"] == jid]})
        threads[a] = {"nodes": nodes, "open_bets": [{"job": b["job"], "variant": b["variant"], "side": b["side"], "usd": b["usd"], "agent": b["agent"],
                                                       "self": "self" in b.get("tags", [])} for b in book.bets if b["agent"] in fam and (b["job"], b["variant"]) not in book.settled]}

    results = st.results
    n = lambda v: sum(1 for r in results if r["verdict"] == v)
    story = {"jobs": len(results), "pass": n("pass"), "fail": n("fail"), "other": len(results) - n("pass") - n("fail"),
             "findings": len(st.findings), "refutations": sum(1 for e in st.edges if e["type"] == "refutes"), "usd": round(sum(r["cost"]["usd"] for r in results), 4)}
    escrow = sum(b["usd"] for b in book.bets if (b["job"], b["variant"]) not in book.settled)
    return {"agents": agents, "markets": markets, "tape": events[-TAPE:][::-1], "lanes": lane_out, "calibration": _calibration(rows), "threads": threads,
            "top": {"mint_per_h": sum(l.get("usd_per_h", 0) for l in lanes.values()), "house": round(book.flows.get(B.HOUSE, 0.0), 4), "escrow": round(escrow, 4),
                    "spend_today": spend, "story": story},
            "rows": len(rows), "row_ts": [r["ts"] for r in rows], "now": now.isoformat(timespec="seconds"), "generated_at": L.now()}


def _age(ts, now):
    return max(0, int((now - B.parse_t(ts)).total_seconds())) if ts else None
