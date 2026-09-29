"""`q replay <dir>`: re-run a night's hand-run jobs through the real fold + default ranking + stop rules
on simulated lanes, and compare with what actually happened.

<dir>/*.toml are job specs with an extra [actual] table:
  lane, start, end (ISO Z) or wall_s, usd, verdict, result = {...} (for templated dependants),
  hand_run = false for a job that only exists in the simulation (a stop-rule follow-up),
  stop_at_s + stop_rule + usd_at_stop + stop_note when a stop rule would have fired.
<dir>/findings.toml: [[node]] id, kind, text for hypotheses that existed before the night; optionally Pit rows:
  [[agent]] id, brief (paid `income_min` minutes of drip before the night), [[bet]] job, variant, side, usd, agent
  (placed as the night opens), [[reflect]] note (appended after the last result). *.toml without [actual] are skipped.
Lanes run one job at a time (their `slots`); `any` jobs run beside them. Markets settle as results land.
"""
import json
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import lanes, ledger as L, book as B, spec as specmod


def ts(t: datetime) -> str:
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load(d: Path):
    jobs = {}
    for f in sorted(d.glob("*.toml")):
        if f.name == "findings.toml":
            continue
        raw = tomllib.loads(f.read_text())
        if "actual" not in raw:
            continue                      # e.g. the example root's lanes.toml
        act = raw.pop("actual")
        if "start" in act:
            act["start_t"], act["end_t"] = parse_t(act["start"]), parse_t(act["end"])
            act.setdefault("wall_s", (act["end_t"] - act["start_t"]).total_seconds())
        jobs[raw["id"]] = (specmod.normalize(raw), act)
    extra = tomllib.loads((d / "findings.toml").read_text()) if (d / "findings.toml").exists() else {}
    return jobs, extra.get("node", []) + [{"t": k, **x} for k in ("agent", "bet", "reflect") for x in extra.get(k, [])]


def simulate(jobs: dict, nodes: list, cfg: dict):
    t0 = min(a["start_t"] for _, a in jobs.values() if a.get("hand_run", True))
    lg = L.MemLedger()
    pre = [n for n in nodes if n.get("t") == "agent"]
    for n in pre:
        lg.append(B.agent_row(B.Book(lg.rows()), n["id"], n["brief"], n.get("parent")), ts(t0 - timedelta(hours=1)))
    if pre:
        B.tick(lg, cfg, t0, since=ts(t0 - timedelta(minutes=max(n.get("income_min", 60) for n in pre))))
    for n in nodes:
        if "t" not in n:
            lg.append({"t": "node", "kind": n.get("kind", "hypothesis"), "id": n["id"], "text": n["text"]}, ts(t0))
    avail = {}
    for jid, (s, a) in jobs.items():
        lg.append({"t": "node", "kind": "job", "id": jid, "spec": s}, ts(t0))
        # a root job was triggered from outside (a deploy, a human); it can't start before it really did
        avail[jid] = a["start_t"] if not s["depends_on"] and "start_t" in a else t0
    for b in (n for n in nodes if n.get("t") == "bet"):
        rows = lg.rows()
        lg.append(B.bet_row(B.Book(rows), L.fold(rows), b["job"], b.get("variant", "main"), b["side"], b["usd"],
                              b["agent"], auto=b.get("auto", False)), ts(t0))
    t, running, sim = t0, {}, {}
    while True:
        st = L.fold(lg.rows())
        ranked, fallback = B.order(st, lg.rows(), cfg)
        for jid in ranked:
            s, a = jobs[jid]
            busy = sum(1 for r in running.values() if r["lane"] == s["lane"])
            if avail[jid] > t or (s["lane"] != "any" and busy >= cfg["lanes"][s["lane"]].get("slots", 1)):
                continue
            stop = a.get("stop_at_s") if a.get("stop_rule") in s["fail_on"] else None
            wall = stop or a["wall_s"]
            lg.append({"t": "claim", "job": jid, "lane": s["lane"], "cid": f"sim:{jid}",
                       **({"fallback": True} if jid in fallback else {})}, ts(t))
            running[jid] = {"lane": s["lane"], "end": t + timedelta(seconds=wall), "stop": stop}
            sim[jid] = {"start": t, "end": running[jid]["end"], "wall": wall, "stopped": bool(stop),
                        "usd": a["usd_at_stop"] if stop else a["usd"]}
        waits = [avail[j] for j in L.fold(lg.rows()).frontier() if avail[j] > t]
        if not running and not waits:
            break
        t = min([r["end"] for r in running.values()] + waits)
        for jid in [j for j, r in running.items() if r["end"] <= t]:
            s, a = jobs[jid]
            r = running.pop(jid)
            verdict = "fail" if r["stop"] else a["verdict"]
            cost = lanes.cost_line(cfg, s["lane"], wall_s=sim[jid]["wall"])
            cost["usd"] = sim[jid]["usd"]   # the documented $ (tokens included), cut at the stop when one fired
            note = f"{a['stop_rule']}: {a.get('stop_note', '')}" if r["stop"] else ""
            lg.append({"t": "result", "job": jid, "verdict": verdict, "cost": cost,
                       "result": a.get("result", {}), "note": note}, ts(t))
            L.settle(lg, s, verdict, ts(t))
            B.settle_due(lg, cfg)
    for n in (n for n in nodes if n.get("t") == "reflect"):
        lg.append({"t": "reflect", "note": n["note"], "rows_covered": len(lg.rows()), **({"agent": n["agent"]} if n.get("agent") else {})}, ts(t))
    return t0, sim, lg


def hm(t: datetime) -> str:
    return t.strftime("%H:%MZ")


def report(jobs, t0, sim, st, rows=()) -> str:
    hand = {j: a for j, (s, a) in jobs.items() if a.get("hand_run", True)}
    out = ["hand-run order: " + " -> ".join(sorted(hand, key=lambda j: hand[j]["start_t"])),
           "simulated order: " + " -> ".join(sorted(sim, key=lambda j: (sim[j]["start"], j))), "",
           f"{'job':<14} {'lane':<11} {'hand':<15} {'sim':<15} {'hand $':>8} {'sim $':>8}  note"]
    for jid in sorted(jobs, key=lambda j: (sim[j]["start"] if j in sim else parse_t("2999-01-01T00:00:00Z"), j)):
        s, a = jobs[jid]
        h = f"{hm(a['start_t'])}-{hm(a['end_t'])}" if jid in hand else "(inside sweep)"
        m = f"{hm(sim[jid]['start'])}-{hm(sim[jid]['end'])}" if jid in sim else "not run"
        note = ("STOPPED " + a.get("stop_rule", "")) if sim.get(jid, {}).get("stopped") else \
            (f"STALE: {st.stale[jid]['refuted']} refuted at {hm(parse_t(st.stale[jid]['ts']))}" if jid in st.stale else "")
        out.append(f"{jid:<14} {s['lane']:<11} {h:<15} {m:<15} {a['usd'] if jid in hand else 0:>8.2f} "
                   f"{sim[jid]['usd'] if jid in sim else 0:>8.2f}  {note}")
    hand_end = max(a["end_t"] for a in hand.values())
    sim_end = max(v["end"] for v in sim.values())
    hand_usd = sum(a["usd"] for a in hand.values())
    sim_usd = sum(v["usd"] for v in sim.values())
    out += ["", f"critical path wall (first start -> last end): hand {(hand_end - t0).total_seconds() / 60:.0f} min "
            f"(ends {hm(hand_end)}), sim {(sim_end - t0).total_seconds() / 60:.0f} min (ends {hm(sim_end)})"]
    for lane in sorted({s["lane"] for s, _ in jobs.values()}):
        he = [a["end_t"] for j, a in hand.items() if jobs[j][0]["lane"] == lane]
        se = [v["end"] for j, v in sim.items() if jobs[j][0]["lane"] == lane]
        if he and se:
            out.append(f"  {lane:<11} last result: hand {hm(max(he))}, sim {hm(max(se))} "
                       f"({(max(he) - max(se)).total_seconds() / 60:+.0f} min earlier in sim)")
    out.append(f"$ spent: hand ${hand_usd:.2f}, sim ${sim_usd:.2f} (saved ${hand_usd - sim_usd:.2f})")
    out.append("where the rules saved:")
    for jid, v in sim.items():
        a = jobs[jid][1]
        if v["stopped"]:
            carried = sum(b["usd"] for j, (s2, b) in jobs.items() if not b.get("hand_run", True)
                          and jid in s2["depends_on"])
            out.append(f"  {jid}: {a['stop_rule']} stop at {a['stop_at_s']:.0f}s of {a['wall_s']:.0f}s "
                       f"-> FAIL with the report ({a.get('stop_note', '')}); the cut tail was "
                       f"${a['usd'] - a['usd_at_stop'] - carried:.2f}, the rest (${carried:.2f}) moves to a follow-up")
    for jid, s in sorted(st.stale.items()):
        if jid in jobs:
            a = jobs[jid][1]
            refuter = s["by"]
            hand_ref = jobs[refuter][1]["end_t"] if refuter in jobs else None
            ran = "ran anyway, then flagged" if jid in sim else "never claimed"
            late = " after the refutation" if hand_ref and a.get("start_t") and a["start_t"] >= hand_ref else ""
            out.append(f"  {jid}: stale when {refuter} refuted {s['refuted']} (sim {hm(parse_t(s['ts']))}, "
                       f"hand {hm(hand_ref) if hand_ref else '?'}); {ran}; the hand run spent ${a['usd']:.2f} "
                       f"on it at {hm(a['start_t'])}-{hm(a['end_t'])}{late}")
    book = B.Book(list(rows))
    if book.agents:
        out.append("wallets after settlement: " + ", ".join(f"{w} ${u:.2f}" for w, u in book.balances().items()))
    return "\n".join(out)


def run(d, root):
    cfg = lanes.load(root)
    jobs, nodes = load(Path(d))
    t0, sim, lg = simulate(jobs, nodes, cfg)
    st = L.fold(lg.rows())
    return jobs, t0, sim, st, report(jobs, t0, sim, st, lg.rows())


def main(d, root, out=None):
    jobs, t0, sim, st, text = run(d, root)
    print(text)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        rows = simulate(*load(Path(d)), lanes.load(root))[2].rows()
        Path(out).write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
        print(f"wrote {len(rows)} rows to {out}")
