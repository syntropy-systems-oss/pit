"""`q reflect`: a plain-text digest of the ledger since the last `reflect` row, for an Opus pass to read."""
import json
from datetime import datetime

from . import ledger as L
from .metrics import metrics, table

MAX_LINES = 400
DEFAULTS = {"rows": 64, "hours": 8, "after": ["cancel", "result:invalid"], "spend_rising": 3}


def since_last(rows: list[dict]) -> list[dict]:
    """Rows after the last reflect row (reflect rows themselves never count)."""
    last = max((i for i, r in enumerate(rows) if r["t"] == "reflect"), default=-1)
    return rows[last + 1:]


# A predicate reads (rows since the last reflect, metrics(all rows), now, its config value) and returns (reason if it fired else None, reading).
WORK = ("node", "result", "cancel", "decision", "claim", "bet", "edge")


def counted(rows: list[dict]) -> list[dict]:
    """Work rows only (not drip, auto, sleep, agent, settle), minus bag-tagged nodes/bets/claims and `invalid` results."""
    bag = {r["id"] for r in rows if r["t"] == "node" and r.get("kind") == "job" and r["spec"].get("bag")}
    return [r for r in rows if r["t"] in WORK and not (r["t"] in ("node", "bet", "claim") and (
            r.get("id") in bag or r.get("job") in bag or "bag" in r.get("tags", []))) and not (r["t"] == "result" and r["verdict"] == "invalid")]


def _rows(rows, m, now, n):
    k = len(counted(rows))
    return (f"rows {k}" if k >= n else None), f"rows: {k}/{n} counted ({len(rows)} raw)"


def _hours(rows, m, now, h):   # clock starts at the first row since the last reflect (the reflect row itself is dropped)
    age = (now - datetime.fromisoformat(rows[0]["ts"])).total_seconds() / 3600 if rows else 0
    return (f"hours {age:.1f}" if age >= h else None), f"hours: {age:.1f}/{h}"


def _after(rows, m, now, specs):  # "cancel", "decision", "result:<verdict>", "node:<kind>"
    hit = next((f"{r['t']}:{r.get('verdict') or r.get('kind')}" if ":" in s else r["t"] for r in rows for s in specs
                if r["t"] == s.split(":")[0] and (":" not in s or s.split(":")[1] == (r.get("verdict") or r.get("kind")))), None)
    return (f"after {hit}" if hit else None), f"after {'|'.join(specs)}: {hit or 'none'}"


def _spend_rising(rows, m, now, k):   # ponytail: a re-run job reads as its latest cost; per-result costs would need the rows
    last = {r["job"]: r for r in rows if r["t"] == "result"}
    usd = [m[r["job"]]["cost"] for r in rows if r["t"] == "result" and r["job"] in m and r is last.get(r["job"])]
    run = 1 if usd else 0
    for i in range(len(usd) - 1, 0, -1):
        if usd[i] <= usd[i - 1]:
            break
        run += 1
    tail = " → ".join(f"${u:.0f}" for u in usd[-min(run, k):]) if run > 1 else ""
    reading = f"spend_rising: {min(run, k)}/{k}" + (f" ({tail})" if tail else "")
    return (f"spend rising {k} results: " + " → ".join(f"${u:.2f}" for u in usd[-k:]) if run >= k else None), reading


PREDICATES = {"rows": _rows, "hours": _hours, "after": _after, "spend_rising": _spend_rising}


def config(cfg: dict) -> dict:
    return cfg.get("reflect", DEFAULTS)


def due(rows: list[dict], now: datetime, cfg: dict) -> str | None:
    """rows = the whole ledger. The reason the first firing predicate gives, or None. A missing [reflect] means DEFAULTS."""
    m, since = metrics(rows), since_last(rows)
    return next((why for k, v in config(cfg).items() if k in PREDICATES and (why := PREDICATES[k](since, m, now, v)[0])), None)


def why(rows: list[dict], now: datetime, cfg: dict) -> str:
    m, since = metrics(rows), since_last(rows)
    return "\n".join(PREDICATES[k](since, m, now, v)[1] for k, v in config(cfg).items() if k in PREDICATES)


def nudge(root, reason: str | None, count: int) -> bool:
    """True once per (reason, row count): the marker file remembers what was already nudged."""
    m = root / "ledger" / ".reflect-nudged"
    key = f"{reason} {count}"
    if not reason or (m.exists() and m.read_text().strip() == key):
        return False
    m.write_text(key)
    return True


def digest(rows: list[dict], n: int | None = None) -> str:
    st = L.fold(rows)
    win = rows[-n:] if n else since_last(rows)
    win = [r for r in win if r["t"] != "reflect"]
    touched = {r.get("id") or r.get("job") for r in win if r["t"] in ("node", "result", "cancel", "claim")}
    out = [f"reflect digest: {len(win)} rows, {win[0]['ts'] if win else '-'} .. {win[-1]['ts'] if win else '-'}"]
    for jid, j in st.jobs.items():
        if jid not in touched:
            continue
        s, res = j["spec"], j["result"]
        line = f"job {jid} [{j['state']}] lane={s['lane']} budget=${s['budget_usd']} expect={s['expect']} " \
               f"q={s['question']}"
        if res:
            c = res["cost"]
            line += f" -> {res['verdict']} ${c['usd']:.2f} {c['wall_s']:.0f}s {res.get('note', '')}"
        if j["cancel"]:
            line += f" | cancelled: {j['cancel']}"
        out.append(line)
    for r in win:
        if r["t"] == "node" and r["kind"] != "job":
            out.append(f"{r['kind']} {r['id']} (from {r.get('from')}): {r.get('text', '')}")
        elif r["t"] == "decision":
            out.append(f"decision {r['finding']} {'CHANGED' if r['changed'] else 'unchanged'}: {r.get('note', '')}")
        elif r["t"] == "cancel":
            out.append(f"cancel {r['id']}: {r.get('reason', '')}")
    sp: dict[str, float] = {}
    for r in win:
        if r["t"] == "result" and st.jobs.get(r["job"], {}).get("result", r) is r:
            sp[r["cost"]["lane"]] = sp.get(r["cost"]["lane"], 0) + r["cost"]["usd"]
    out.append("metrics:\n" + table(metrics(rows), touched))
    out.append("spend by lane: " + (", ".join(f"{k} ${v:.2f}" for k, v in sorted(sp.items())) or "$0"))
    front = st.frontier()
    for r in rows:
        if r["t"] == "auto" and r["type"] == "note" and r["reason"] == "desk-stalled" and st.jobs.get(r["job"], {}).get("state") == "queued":
            out.append(f"desk-stalled {r['job']}: re-handed 3 times to its proposer with no result")
    out.append("frontier: " + (", ".join(front) or "empty"))
    for jid, j in st.jobs.items():
        if j["state"] == "queued" and jid not in front:
            out.append(f"  blocked {jid}: {'; '.join(st.why_blocked(jid))}")
    out.append("open jobs (full specs):")
    for jid, j in st.jobs.items():
        if j["state"] in ("queued", "running"):
            out.append(f"  {jid} [{j['state']}] " + json.dumps(j["spec"], sort_keys=True))
    if len(out) > MAX_LINES:      # keep the header and the tail (spend, frontier, open specs); drop oldest middle
        cut = len(out) - MAX_LINES + 1
        out = out[:1] + [f"... {cut} oldest lines truncated"] + out[1 + cut:]
    return "\n".join(out)
