"""The bag: when a lane has nothing runnable, the house draws a known-good scenario and runs it.

`[bag.<lane>]` in lanes.toml (a spec may carry its own `max_per_day`): specs (a path or glob, or a list of them, relative to the state root), max_per_day,
budget_usd (fallback when the spec has none), house_stake (usd PASS per variant), enabled.
A draw is posted as proposer `house` (spec `bag = true`, `bag_spec = <the file's id>`; no wallet pays its budget, like a
seed) with a house PASS `bet` tagged `bag` (from the vig pool, capped by it; left out of calibration). A FAIL becomes a
finding "REGRESSION: ..." naming the spec's previous pass.
"""
import glob
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from . import book as B, ledger as L, spec as specmod

DEFAULTS = {"enabled": True, "max_per_day": 6, "house_stake": 0.25, "specs": [], "backoff_minutes": 30}


def conf(cfg: dict, lane: str) -> dict | None:
    c = cfg.get("bag", {}).get(lane)
    glob_ = {k: v for k, v in cfg.get("bag", {}).items() if not isinstance(v, dict)}     # every scalar [bag] key, not just backoff
    return {**DEFAULTS, **glob_, **c} if isinstance(c, dict) else None


def files(root, c: dict) -> list[Path]:
    pats = [c["specs"]] if isinstance(c["specs"], str) else c["specs"]
    out = {Path(p) for pat in pats for p in glob.glob(str(Path(root) / Path(pat).expanduser()))}
    return sorted(out)


def bag_jobs(rows: list[dict], lane: str | None = None) -> list[dict]:
    return [r for r in rows if r["t"] == "node" and r.get("kind") == "job" and r["spec"].get("bag")
            and (lane is None or r["spec"]["lane"] == lane)]


def capped(n: int, c: dict, now) -> str:
    """'' below the daily cap; at it, the justified-backoff note with the UTC rollover (the day is the ledger's UTC date)."""
    if n < c["max_per_day"]:
        return ""
    return f" · daily cap reached, resets {B.iso((now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0))}"


def today(rows: list[dict], lane: str, now) -> int:
    """Bag draws that COUNT today: ran to pass/fail or still running; an `invalid` draw (images missing, preflight lied)
    does not spend the day's allowance."""
    day = B.iso(now)[:10]
    verdict = {}
    for r in rows:
        if r.get("t") == "result":
            verdict[r["job"]] = r.get("verdict")
    return sum(1 for r in bag_jobs(rows, lane) if r["ts"][:10] == day and verdict.get(r["id"]) != "invalid")


def active(st: L.State, lane: str) -> list[str]:
    return [j for j, x in st.jobs.items() if x["spec"].get("bag") and x["spec"]["lane"] == lane and x["state"] in ("queued", "running")]


def results(rows: list[dict], spec_id: str) -> list[dict]:
    """The results of every bag run of this spec id, oldest first."""
    mine = {r["id"] for r in bag_jobs(rows) if r["spec"]["bag_spec"] == spec_id}
    return [r for r in rows if r["t"] == "result" and r["job"] in mine]


def last_run(rows: list[dict], spec_id: str, scenario: str | None = None, lane: str | None = None) -> str | None:
    """Latest result of this spec's bag runs, or of any job (any id) running the same scenario on the same lane."""
    mine = {r["id"] for r in bag_jobs(rows) if r["spec"]["bag_spec"] == spec_id}
    if scenario:
        mine |= {r["id"] for r in rows if r["t"] == "node" and r.get("kind") == "job" and r["spec"].get("scenario") == scenario and r["spec"].get("lane") == lane}
    return max((r["ts"] for r in rows if r["t"] == "result" and r["job"] in mine), default=None)


def last_pass(rows: list[dict], spec_id: str, before: str = "￿") -> str | None:
    return next((r["ts"] for r in reversed(results(rows, spec_id)) if r["verdict"] == "pass" and r["ts"] < before), None)


def spec_today(rows: list[dict], spec_id: str, now) -> int:
    return sum(1 for r in bag_jobs(rows) if r["spec"]["bag_spec"] == spec_id and r["ts"][:10] == B.iso(now)[:10])


def consecutive_fails(rows: list[dict], spec_id: str) -> int:
    """Trailing consecutive FAIL results of this spec's bag jobs (invalid rows are skipped; a pass or a
    `bag-readmit <spec>` note ends the streak)."""
    readmit = [r["ts"] for r in rows if r.get("t") == "auto" and r.get("type") == "note" and r.get("reason") == f"bag-readmit {spec_id}"]
    since = readmit[-1] if readmit else ""
    ids = {r["id"] for r in rows if r.get("t") == "node" and r.get("kind") == "job" and (r.get("spec") or {}).get("bag_spec") == spec_id}
    k = 0
    for r in reversed([r for r in rows if r.get("t") == "result" and r.get("job") in ids and r["ts"] > since]):
        if r.get("verdict") not in ("fail", "invalid"):      # a rung that can never score is quarantined too
            break
        k += 1
    return k


def draw(root, rows: list[dict], cfg: dict, lane: str, c: dict, now, echo=print) -> tuple[dict, str | None] | None:
    """(spec, its last result ts): least recently run first, never-run first. ponytail: ties (all never run, or the same
    ts) break by spec id; there is no cheap "touched by the latest change" signal (the specs do not name the files
    they cover), upgrade path: a `touches = [...]` list per spec checked against `git diff` of the checkout under test."""
    cands, known = [], specmod.scenarios(root, cfg)
    bo = backoff_until(rows, lane, c)                                # one cause hits every spec on the lane: the invalid streak is lane-wide,
    lane_inv = bo[1] if bo and bo[0] > now else 0                    # but only while its backoff runs, so an expired backoff lifts it
    for p in files(root, c):
        s = specmod.load(p)
        errs = specmod.validate({**({"budget_usd": c["budget_usd"]} if "budget_usd" in c else {}), **s, "lane": lane, "id": s.get("id", p.stem)}, cfg["lanes"], known, cfg.get("bench", {}).get("drivers"))
        if errs:
            echo(f"bag {lane}: skip {p.name}: {'; '.join(errs)}")
        elif s.get("max_per_day") is not None and spec_today(rows, s["id"], now) >= s["max_per_day"]:
            continue          # a spec's own cap (the live smoke rung: 2 a day)
        elif (last_run(rows, s["id"], s.get("scenario"), lane) or "") > B.iso(now - timedelta(minutes=c.get("min_interval_minutes", 120))):
            continue          # ran recently: a thin rotation must not re-run the same check every few minutes
        elif max(consecutive_fails(rows, s["id"]), lane_inv) >= c.get("quarantine_after", 3):
            echo(f"bag {lane}: {s['id']} quarantined ({max(consecutive_fails(rows, s['id']), lane_inv)} consecutive fails/invalids; a pass, `bag-readmit` or `bag-reset` note reopens it)")
        else:
            cands.append((last_run(rows, s["id"], s.get("scenario"), lane) or "", s["id"], s))
    if not cands:
        return None
    ts, _, s = min(cands, key=lambda x: x[:2])
    return s, ts or None


def post(lg: L.Ledger, cfg: dict, s: dict, lane: str, c: dict, stamp: str) -> dict:
    """Append the house's node and its PASS stake(s); returns the posted spec."""
    taken = {r["id"] for r in bag_jobs(lg.rows())}
    jid = next(j for j in (f"bag-{s['id']}-{stamp}", *(f"bag-{s['id']}-{stamp}-{n}" for n in range(2, 99))) if j not in taken)
    spec = {**({"budget_usd": c["budget_usd"]} if "budget_usd" in c else {}),      # lane budget is the fallback; the spec's own wins
            **s, "id": jid, "lane": lane, "expect": "pass", "proposer": B.HOUSE, "bag": True, "bag_spec": s["id"]}
    lg.append({"t": "node", "kind": "job", "id": jid, "spec": spec})
    for v in B.variants(spec):
        usd = round(min(c["house_stake"], B.Book(lg.rows()).flows.get(B.HOUSE, 0.0)), 4)
        if usd > 0:       # an empty vig pool cannot stake: the run still goes ahead
            lg.append({"t": "bet", "job": jid, "variant": v, "side": "pass", "usd": usd, "agent": B.HOUSE,
                       "book": B.HOUSE, "tags": ["bag"]})
    return spec


def regressions(lg: L.Ledger, echo=print) -> list[str]:
    """A finding for every failed bag run that has none yet."""
    rows = lg.rows()
    st = L.fold(rows)
    out = []
    for j, x in st.jobs.items():
        res = x["result"]
        if not x["spec"].get("bag") or not res or res["verdict"] != "fail" or any(f["from"] == j for f in st.findings.values()):
            continue
        sid, prev = x["spec"]["bag_spec"], last_pass(rows, x["spec"]["bag_spec"], res["ts"])
        fid = f"F:{j}-1"
        text = (f"REGRESSION: {sid} failed at {res['ts']} on {x['spec']['lane']} ({res.get('note') or 'verdict fail'}); "
                f"previous pass {prev or 'none on record'}: the regression is between the two")
        lg.append({"t": "node", "kind": "finding", "id": fid, "from": j, "text": text, "agent": B.HOUSE})
        lg.append({"t": "edge", "type": "produces", "from": j, "to": fid})
        echo(f"{fid}: {text}")
        out.append(fid)
    return out


def backoff_until(rows: list[dict], lane: str, c: dict) -> tuple[datetime, int] | None:
    """(when the lane may draw again, consecutive invalids) after an `invalid` bag result; a fail or pass (or none) = no backoff.
    Wait = backoff_minutes x 2^(k-1), capped at 4 h."""
    ids = {r["id"] for r in bag_jobs(rows, lane)}
    resets = [r["ts"] for r in rows if r.get("t") == "auto" and r.get("type") == "note" and r.get("reason") == f"bag-reset {lane}"]
    since = resets[-1] if resets else ""
    vs = [r for r in rows if r["t"] == "result" and r["job"] in ids and r["ts"] > since]
    k = 0
    for r in reversed(vs):
        if r["verdict"] != "invalid":
            break
        k += 1
    if not k:
        return None
    return datetime.fromisoformat(vs[-1]["ts"]) + timedelta(minutes=min(c["backoff_minutes"] * 2 ** (k - 1), 240)), k


def preflight(root, s: dict, timeout: int = 300) -> str | None:
    """None if the spec declares no preflight or it exits 0; else why not (its last output line)."""
    cmd = s.get("preflight")
    if not cmd:
        return None
    try:
        p = subprocess.run(cmd, shell=True, cwd=Path(s.get("cwd") or root).expanduser(), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout}s"
    if p.returncode == 0:
        return None
    out = (p.stdout + p.stderr).strip().splitlines()
    return out[-1] if out else f"exit {p.returncode}"
