"""Derived per-node metadata, computed from the ledger on demand. Nothing here is written back."""
import bisect
from . import ledger as L, spec as specmod


def metrics(rows: list[dict]) -> dict[str, dict]:
    """{node id: {...}}. Job: state verdict cost wall_s cost_ratio lineage_spend depth rows_since_added. Finding: kind status open_dependents."""
    st = L.fold(rows)
    out: dict[str, dict] = {}
    stamps = sorted(r["ts"] for r in rows)          # one sort; rows_since_added per job was a scan of every row per job
    for jid, j in st.jobs.items():
        s, c = j["spec"], (j["result"] or {}).get("cost", {})
        out[jid] = {"state": j["state"], "verdict": j["result"]["verdict"] if j["result"] else None,
                    "cost": c.get("usd", 0.0), "wall_s": c.get("wall_s", 0.0),
                    "cost_ratio": c.get("usd", 0.0) / s["budget_usd"] if s["budget_usd"] else 0.0,
                    "rows_since_added": len(stamps) - bisect.bisect_right(stamps, j["added"])}
    depth: dict[str, int] = {}

    def d(jid, seen=()):       # longest depends-on chain to a root; a cycle counts as a root
        if jid not in depth:
            up = [specmod.dep_id(x)[0] for x in st.jobs[jid]["spec"]["depends_on"]]
            depth[jid] = 1 + max((d(u, seen + (jid,)) for u in up if u in st.jobs and u not in seen and u != jid), default=-1)
        return depth[jid]

    for jid in st.jobs:
        out[jid]["depth"] = d(jid)
        out[jid]["lineage_spend"] = out[jid]["cost"] + sum(out[u]["cost"] for u in st.upstream(jid) if u in st.jobs and u != jid)
    for fid, f in st.findings.items():
        out[fid] = {"kind": f["kind"], "status": f["status"],
                    "open_dependents": sum(1 for k in st.children(fid) if k in st.jobs and st.jobs[k]["state"] in ("queued", "running"))}
    return out


def table(m: dict[str, dict], only=None) -> str:
    ids = [i for i in m if only is None or i in only]
    cols = ["state", "verdict", "cost", "wall_s", "cost_ratio", "lineage_spend", "depth", "rows_since_added"]
    fcols = ["kind", "status", "open_dependents"]
    fmt = lambda v: f"{v:.2f}" if isinstance(v, float) else "-" if v is None else str(v)
    out = ["job          " + " ".join(f"{c:>10}" for c in cols)]
    out += [f"{i:<12} " + " ".join(f"{fmt(m[i][c]):>10}" for c in cols) for i in ids if "depth" in m[i]]
    out += ["finding      " + " ".join(f"{c:>10}" for c in fcols)]
    out += [f"{i:<12} " + " ".join(f"{fmt(m[i][c]):>10}" for c in fcols) for i in ids if "depth" not in m[i]]
    return "\n".join(out)
