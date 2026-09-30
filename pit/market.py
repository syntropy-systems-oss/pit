"""market_json(): what the terminal (view/terminal.html) needs, folded from the (already sliced) ledger. Read-only; reuses ledger.fold and B.Book/order/sleepers/calibration."""
from datetime import datetime, timezone

from . import autopilot, bag, ledger as L, book as B, spec as specmod

TAPE = 200


def _calibration(rows):
    """B.calibration() renders text; read its table back (header-driven) rather than duplicate the maths."""
    head, *body = B.calibration(rows).splitlines()
    keys = head.split()
    return [dict(zip(keys, ln.split())) for ln in body]


def _walk(rows, book, cfg):
    """One pass over the rows: per-wallet balance series, and the one-line tape events."""
    flows, series, tot, closed, done, events, fund = {}, {}, {}, set(), set(), [], {}

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
            ev.update(type="AGENT", agent=r["id"], text=f"{r['kind']}" + (f" under {r['parent']}" if r.get("parent") else "") + (f", planted by {r['by']}" if r.get("by") else "") + f": {r['brief'][:80]}")
        elif t == "retire":
            ev.update(type="AGENT", agent=r["agent"], text=f"retired{' by ' + r['by'] if r.get('by') else ''}: {r['reason'][:80]}")
        elif t == "node" and r.get("kind") == "job":
            s, who = r["spec"], r["spec"].get("proposer")
            if book.payer(s):
                move(book.payer(s), -s.get("budget_usd", 0), i)
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
            c, f = r.get("cost", {}), r.get("funding")
            if f:      # the unspent funding back (+) or the overage (-); a corrected result replaces its earlier row, like Book
                move(f["wallet"], f["usd"] - fund.get(r["job"], 0.0), i)
                fund[r["job"]] = f["usd"]
            ev.update(type="RESULT", verdict=r["verdict"], usd=c.get("usd", 0), text=f"{r['job']}  {r['verdict'].upper()}  ${c.get('usd', 0):.2f}  {c.get('wall_s', 0):.0f}s {c.get('lane', '')}" + (f"  by {r['agent']}" if r.get("agent") else ""))
        elif t == "settle":
            if (r["job"], r["variant"]) not in done or r.get("supersedes"):   # settle rows count once, like Book, unless re-settled
                done.add((r["job"], r["variant"]))
                for w, u in r.get("clawback", {}).items():
                    move(w, -u, i)
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
                "question": s["question"], "if_pass": s.get("if_pass", ""), "if_fail": s.get("if_fail", ""),
                "read": specmod.is_read(s), "then": s.get("then", ""),
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
                     "funded_s": specmod.funded_seconds(v["spec"], cfg["lanes"]), "budget_usd": v["spec"]["budget_usd"],
                     "burned_usd": round(_age(v["claim"]["ts"], now) / 3600 * l.get("usd_per_h", 0), 4), "fallback": bool(v["claim"].get("fallback"))}
                    for j, v in st.jobs.items() if v["state"] == "running" and v["spec"]["lane"] == name), None)
        q = [j for j in order if st.jobs[j]["spec"]["lane"] == name]
        lane_out.append({"lane": name, "price": l.get("usd_per_h", 0), "box": l.get("box", ""), "running": run,
                         "idle_s": None if run else autopilot.lane_idle(rows, st, name, now),
                         "depth": len(q), "queue": [{"task": j, "score": mk[j]["score"], "matched": mk[j]["matched"], "budget": mk[j]["budget"],
                                                     "fallback": j in fb, "proposer": mk[j]["proposer"], "read": mk[j]["read"]} for j in q], "spend_today": spend.get(name, 0.0),
                         "bag": (lambda c: c and {"n": bag.today(rows, name, now), "max": c["max_per_day"]})(bag.conf(cfg, name))})

    sleepers, fams = B.sleepers(rows), {}
    agents = []
    subs = [r for r in book.agents.values() if r.get("parent")]           # per-turn identities: presentation folds them into their root
    for a, r in book.agents.items():
        if r.get("parent"):
            continue
        z = sleepers.get(a)
        w = book.wallet(a)
        turns = [s["ts"] for s in subs if book.wallet(s["id"]) == a]
        agents.append({"id": a, "kind": r["kind"], "parent": None, "brief": r["brief"], "wallet": w,
                       "runtime": runtime_label(r, cfg, a), "balance": round(book.balance(a), 4),
                       "turns": len(turns), "last_turn": max(turns, default=None), "last_turn_age_s": _age(max(turns, default=None), now),
                       "series": series.get(w, [])[-80:], "bootstrap_cost": B.bootstrap_cost(rows, a), "last_acted": autopilot.last_acted(rows, book, a), "sleeping": bool(z and not z["wake"]),
                       "retired": (lambda x: x and {"reason": x["reason"], "ts": x["ts"], "by": x.get("by")})(book.retired.get(a)),
                       "sleep": None if not z else {"until": B.until_text(z["sleep"]["until"]), "note": z["sleep"].get("note", ""), "since": z["sleep"]["ts"],
                                                     "woke": z["wake"]["reason"] if z["wake"] else None}})
    claimed = {}
    for r in rows:
        if r["t"] == "claim" and r.get("agent"):
            claimed.setdefault(r["agent"], set()).add(r["job"])
    threads = {}
    for a in (x["id"] for x in agents):
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
            "top": {"mint_per_h": round(B.conf(cfg)["mint"] * sum(l.get("usd_per_h", 0) for l in lanes.values()), 2), "house": round(book.flows.get(B.HOUSE, 0.0), 4), "escrow": round(escrow, 4),
                    "spend_today": spend, "story": story, "subs": len(subs), "newcomer_cost": B.newcomer_cost(rows)},
            "autopilot": autopilot.status(rows, cfg, now), "rows": len(rows), "row_ts": [r["ts"] for r in rows], "now": now.isoformat(timespec="seconds"), "generated_at": L.now()}


def _age(ts, now):
    return max(0, int((now - B.parse_t(ts)).total_seconds())) if ts else None


def runtime_label(row: dict, cfg: dict, agent: str) -> str:
    """'<runtime>/<model>' for an agent row; the model falls back to the autopilot's default for that runtime
    (and to the reflection runtime/model for the reflect agent), so the terminal always names the model in use."""
    ap = (cfg or {}).get("autopilot", {})
    rt = row.get("runtime") or ("claude")
    model = row.get("model")
    if not model:
        if agent == "reflect":
            rt = ap.get("reflect", {}).get("runtime", rt); model = ap.get("reflect", {}).get("model", "opus")
        else:
            model = ap.get("runtimes", {}).get(rt, {}).get("model") or ("sonnet" if rt == "claude" else "")
    return f"{rt}/{model}" if model else rt
