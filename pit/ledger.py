"""Append-only ndjson ledger (one file per host) and fold(): rows -> graph, frontier, stale-by-refutation.

Row types (`t`): node, edge, claim, release, result, decision, cancel, review, reflect (ignored by fold).
Edge types: depends-on, produces, edits, refutes, refines, supersedes.
"""
import json
import socket
import statistics
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import spec as specmod
from .lanes import rate

EDGE_TYPES = ("depends-on", "produces", "edits", "refutes", "refines", "supersedes")
SETTLED = ("pass", "fail")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Ledger:
    """`ledger/<host>.ndjson`. Each host only appends to its own file, so git merges never conflict."""

    def __init__(self, directory, host: str | None = None):
        self.dir = Path(directory)
        self.host = host or socket.gethostname().split(".")[0]

    @property
    def path(self) -> Path:
        return self.dir / f"{self.host}.ndjson"

    def append(self, row: dict, ts: str | None = None) -> dict:
        row = {"ts": ts or now(), "host": self.host, **row}
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
        return row

    def rows(self) -> list[dict]:
        out = []
        for f in sorted(self.dir.glob("*.ndjson")):
            for i, line in enumerate(f.read_text().splitlines()):
                if line.strip():
                    out.append((json.loads(line), f.name, i))
        out.sort(key=lambda r: (r[0]["ts"], r[1], r[2]))
        return [r[0] for r in out]


class MemLedger(Ledger):
    """Same interface, in memory (replay, tests)."""

    def __init__(self, host: str = "sim"):
        self.host, self._rows = host, []

    def append(self, row, ts=None):
        row = {"ts": ts or now(), "host": self.host, **row}
        self._rows.append(row)
        return row

    def rows(self):
        return sorted(self._rows, key=lambda r: r["ts"])


# ---- fold -----------------------------------------------------------------------------------------

class State:
    def __init__(self):
        self.jobs: dict[str, dict] = {}        # id -> {spec, added, state, claim, result, cancel}
        self.findings: dict[str, dict] = {}    # id -> {kind, text, status, from, ts}
        self.edges: list[dict] = []            # {type, from, to, ts}
        self.decisions: list[dict] = []
        self.results: list[dict] = []          # the last result row per job (earlier ones stay in the ledger as history)
        self.stale: dict[str, dict] = {}       # node id -> {by, refuted, ts}

    # graph ------------------------------------------------------------------
    def all_edges(self) -> list[dict]:
        """Explicit edge rows plus what job specs declare (depends-on, edits from templates)."""
        out = list(self.edges)
        for jid, j in self.jobs.items():
            s = j["spec"]
            for d in s["depends_on"]:
                out.append({"type": "depends-on", "from": jid, "to": specmod.dep_id(d)[0], "ts": j["added"]})
            for expr in specmod.refs(s.get("inputs", {})):
                m = specmod.REF.match(expr)
                if m and (m[2] or m[5]):
                    out.append({"type": "edits", "from": m[2] or m[5], "to": jid, "ts": j["added"]})
        return out

    def adjacency(self) -> tuple[dict, dict]:
        """(children, parents) of every node, built once from all_edges and kept while the graph is unchanged. metrics()
        asks for children and parents thousands of times per tick; rebuilding the edge list each time was quadratic."""
        key = (len(self.edges), len(self.jobs))
        if getattr(self, "_adj_key", None) != key:
            kids, parents = {}, {}
            for e in self.all_edges():
                t = e["type"]
                if t == "depends-on":
                    kids.setdefault(e["to"], set()).add(e["from"]); parents.setdefault(e["from"], set()).add(e["to"])
                elif t in ("produces", "edits", "refines"):
                    kids.setdefault(e["from"], set()).add(e["to"]); parents.setdefault(e["to"], set()).add(e["from"])
                elif t == "refutes":
                    parents.setdefault(e["to"], set()).add(e["from"])
            self._adj_key, self._kids, self._parents = key, kids, parents
        return self._kids, self._parents

    def children(self, node: str) -> set[str]:
        """Nodes whose validity rests on `node` (the direction staleness flows)."""
        return set(self.adjacency()[0].get(node, ()))

    def downstream(self, node: str, skip: frozenset = frozenset()) -> set[str]:
        seen, todo = set(), [node]
        while todo:
            for k in self.children(todo.pop()):
                if k not in seen and k != node and k not in skip:
                    seen.add(k)
                    todo.append(k)
        return seen

    def upstream(self, node: str) -> set[str]:
        seen, todo = set(), [node]
        parents = self.adjacency()[1]
        while todo:
            n = todo.pop()
            for parent in parents.get(n, ()):
                if parent and parent not in seen:
                    seen.add(parent)
                    todo.append(parent)
        return seen

    # readiness --------------------------------------------------------------
    def dep_reason(self, dep: str) -> str | None:
        """None when the dependency is satisfied, else why not."""
        did, want = specmod.dep_id(dep)
        if did in self.jobs:
            j = self.jobs[did]
            v = j["result"]["verdict"] if j["result"] else None
            if v is None:
                return f"waiting on {did} ({j['state']})"
            if v not in SETTLED and not (v == "read" and not want):      # a read feeds a plain dependency, never a branch
                return f"{did} ended {v}: not evidence; rerun or cancel"
            if want and v != want:
                return f"{did} ended {v}; this job runs only on {want} (dead branch: cancel it)"
            return None
        if did in self.findings:
            f = self.findings[did]
            if f["status"] == "refuted":
                return f"{did} was refuted"
            if f["status"] == "superseded":
                return f"{did} was superseded"
            return None
        return f"waiting on {did} (not in the ledger yet)"

    def render_ctx(self) -> dict:
        return {"jobs": {jid: {"verdict": j["result"]["verdict"] if j["result"] else None,
                               "result": (j["result"] or {}).get("result", {})} for jid, j in self.jobs.items()},
                "findings": self.findings}

    def why_blocked(self, jid: str) -> list[str]:
        if jid not in self.jobs:
            return [f"no job {jid}"]
        j, out = self.jobs[jid], []
        if j["state"] != "queued":
            out.append(f"state is {j['state']}" + (f": {j['cancel']}" if j.get("cancel") else ""))
        if jid in self.stale:
            s = self.stale[jid]
            out.append(f"stale: {s['refuted']} refuted by {s['by']} at {s['ts']} (q review {jid} to re-admit)")
        for rid in specmod.informed_by(self.jobs, jid):      # its bets stay open until the readout has reached them
            if self.jobs[rid]["state"] in ("queued", "running"):
                out.append(f"waits for {rid}, the read that informs it ({self.jobs[rid]['state']})")
        for d in j["spec"]["depends_on"]:
            r = self.dep_reason(d)
            if r:
                did = specmod.dep_id(d)[0]
                if did in self.jobs and self.jobs[did]["state"] == "queued":
                    deeper = self.why_blocked(did)
                    r += "; it is " + ("ready" if not deeper else "blocked: " + deeper[0])
                out.append(r)
        if not out:
            try:
                specmod.render(j["spec"], self.render_ctx())
            except specmod.SpecError as e:
                out.append(str(e))
        return out

    def frontier(self) -> list[str]:
        return [jid for jid, j in self.jobs.items() if j["state"] == "queued" and not self.why_blocked(jid)]

    def running(self) -> list[str]:
        return [jid for jid, j in self.jobs.items() if j["state"] == "running"]

    def spend(self, by: str = "lane") -> dict[str, float]:
        out: dict[str, float] = {}
        for r in self.results:
            k = r["cost"].get("lane", "?") if by == "lane" else r["job"]
            out[k] = round(out.get(k, 0) + r["cost"]["usd"], 4)
        return out


def fold(rows: list[dict]) -> State:
    st = State()
    claims: dict[str, list[dict]] = {}
    released: set[str] = set()
    reviews: dict[str, str] = {}
    for r in rows:
        t = r["t"]
        if t == "node":
            if r["kind"] == "job":
                st.jobs[r["id"]] = {"spec": specmod.normalize(r["spec"]), "added": r["ts"], "state": "queued",
                                    "claim": None, "result": None, "cancel": None}
            else:
                st.findings[r["id"]] = {"kind": r["kind"], "text": r.get("text", ""), "status": "open",
                                        "from": r.get("from"), "ts": r["ts"]}
        elif t == "edge":
            st.edges.append({"type": r["type"], "from": r["from"], "to": r["to"], "ts": r["ts"]})
            if r["type"] == "refutes" and r["to"] in st.findings:
                st.findings[r["to"]]["status"] = "refuted"
            if r["type"] == "supersedes":
                if r["to"] in st.findings:
                    st.findings[r["to"]]["status"] = "superseded"
                if r["to"] in st.jobs:
                    st.jobs[r["to"]]["state"] = "superseded"
                    for f in st.findings.values():
                        if f["from"] == r["to"] and f["status"] == "open":
                            f["status"] = "superseded"
        elif t == "claim":
            claims.setdefault(r["job"], []).append(r)
        elif t == "release":
            released.add(r["cid"])
        elif t == "result":
            st.results = [x for x in st.results if x["job"] != r["job"]] + [r]
            if r["job"] in st.jobs:
                st.jobs[r["job"]]["result"] = r
        elif t == "decision":
            st.decisions.append(r)
        elif t == "cancel":
            j = st.jobs.get(r["id"])
            if j and not j["result"]:
                j["state"], j["cancel"] = "cancelled", r.get("reason", "")
        elif t == "review":
            reviews[r["id"]] = r["ts"]
    # claims: the earliest claim not released wins (a loser of a push race writes a release row)
    for jid, cs in claims.items():
        live = [c for c in cs if c["cid"] not in released]
        j = st.jobs.get(jid)
        if not j or not live:
            continue
        j["claim"], j["claims"] = live[0], live
        if j["state"] == "queued" and not j["result"]:
            j["state"] = "running"
    for j in st.jobs.values():
        if j["result"] and j["state"] in ("queued", "running"):
            j["state"] = "done"
    # refutes: everything downstream of a refuted node is stale until reviewed after the refutation
    for e in st.edges:
        if e["type"] != "refutes":
            continue
        # the refuter (and what rests only on it) is the evidence, not a casualty
        for n in st.downstream(e["to"], skip=frozenset({e["from"]})):
            if reviews.get(n, "") < e["ts"] and n not in st.stale:
                st.stale[n] = {"by": e["from"], "refuted": e["to"], "ts": e["ts"]}
    return st


# ---- settling a result into findings -------------------------------------------------------------

def settle(ledger: Ledger, spec: dict, verdict: str, ts: str | None = None) -> list[dict]:
    """After a pass/fail, write the findings the spec declares for that branch, and its refutations."""
    rows, jid = [], spec["id"]
    if verdict not in SETTLED:
        return rows
    branch = spec["if_pass"] if verdict == "pass" else spec["if_fail"]
    for fid in spec["produces"] + spec[f"produces_if_{verdict}"]:
        rows.append(ledger.append({"t": "node", "kind": "finding", "id": fid, "from": jid,
                                   "text": f"{spec['question']} -> {verdict}: {branch}"}, ts))
        rows.append(ledger.append({"t": "edge", "type": "produces", "from": jid, "to": fid}, ts))
    for target in spec[f"refutes_if_{verdict}"]:
        rows.append(ledger.append({"t": "edge", "type": "refutes", "from": jid, "to": target}, ts))
    return rows


# ---- ranking (the session's default heuristic; Pit never decides for it) ----------------------

def est_wall(st: State, jid: str, cfg: dict | None = None) -> float:
    """Median wall of past results with the same lane and first word of `run`, else the seconds its funding buys."""
    s = st.jobs[jid]["spec"]
    key = (s["lane"], (s.get("run") or "").split(" ")[0])
    walls = [r["cost"]["wall_s"] for r in st.results if r["job"] in st.jobs and r["job"] != jid
             and (st.jobs[r["job"]]["spec"]["lane"], (st.jobs[r["job"]]["spec"].get("run") or "").split(" ")[0])
             == key and s.get("run")]
    return statistics.median(walls) if walls else float(specmod.funded_seconds(s, (cfg or {}).get("lanes", {})))


def next_jobs(st: State, jid: str) -> set[str]:
    """Open jobs that depend on jid directly or on a finding jid's spec produces."""
    s = st.jobs[jid]["spec"]
    parents = {jid, *s["produces"], *s["produces_if_pass"], *s["produces_if_fail"]}
    return {k for k, j in st.jobs.items() if j["state"] in ("queued", "running")
            and parents & {specmod.dep_id(d)[0] for d in j["spec"]["depends_on"]}}


def critical_path(st: State, jid: str, _seen: frozenset = frozenset(), cfg: dict | None = None) -> float:
    """est wall of this job plus the longest chain of open jobs that rest on it."""
    _seen = _seen | {jid}
    return est_wall(st, jid, cfg) + max((critical_path(st, k, _seen, cfg) for k in next_jobs(st, jid) - _seen), default=0.0)


def rank(st: State, cfg: dict, ids: list[str] | None = None) -> list[str]:
    """Default order: priority, then jobs that unblock others (longest critical path first, then the most
    open jobs downstream: that is what frees idle lanes), then value per estimated dollar, then age."""
    ids = st.frontier() if ids is None else ids

    def key(jid):
        s = st.jobs[jid]["spec"]
        has_kids = bool(next_jobs(st, jid))
        est_usd = max(est_wall(st, jid, cfg) / 3600 * rate(cfg, s["lane"]), 0.01)
        n_down = sum(1 for k in st.downstream(jid) if st.jobs.get(k, {}).get("state") == "queued")
        return (-s["priority"], -(critical_path(st, jid, cfg=cfg) if has_kids else 0), -n_down, -s["value"] / est_usd,
                st.jobs[jid]["added"])
    return sorted(ids, key=key)


# ---- claims: git is the lock --------------------------------------------------------------------

def git(root, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def commit(root, ledger: Ledger, msg: str) -> None:
    git(root, "add", str(ledger.path))
    git(root, "commit", "-q", "-m", msg)


def has_remote(root) -> bool:
    return bool(git(root, "remote").stdout.strip())


def push(root, enabled: bool = False) -> bool:
    """True when pushed, or when there is nothing to push to: no remote, or `[git] push` is off (the local commit is the lock)."""
    if not enabled or not has_remote(root):
        return True
    return git(root, "push", "-q").returncode == 0


def claim(root, ledger: Ledger, jid: str, lane: str, tries: int = 3, extra: dict | None = None,
          push_ok: bool = False) -> tuple[bool, str]:
    st = fold(ledger.rows())
    if st.jobs.get(jid, {}).get("state") != "queued":
        return False, f"{jid} is {st.jobs.get(jid, {}).get('state', 'unknown')}"
    cid = f"{ledger.host}:{now()}"
    ledger.append({"t": "claim", "job": jid, "lane": lane, "cid": cid, **(extra or {})})
    commit(root, ledger, f"claim {jid} on {lane}")
    for _ in range(tries):
        if push(root, push_ok):
            return True, cid
        git(root, "pull", "-q", "--rebase")
        j = fold(ledger.rows()).jobs[jid]
        rivals = [c["cid"] for c in j.get("claims", []) if c["cid"] != cid]
        if rivals or j["result"]:
            # their claim reached the remote first, whatever the clocks say
            ledger.append({"t": "release", "job": jid, "cid": cid})
            commit(root, ledger, f"release {jid}: lost the claim")
            push(root, push_ok) or (git(root, "pull", "-q", "--rebase"), push(root, push_ok))
            return False, f"lost the claim on {jid} to {rivals[0] if rivals else 'a finished run'}"
    # a push that fails for any other reason must not strand the claim (and the lane): release it
    ledger.append({"t": "release", "job": jid, "cid": cid})
    commit(root, ledger, f"release {jid}: push kept failing")
    return False, f"claim push kept failing ({tries} tries): released {jid}; check the remote or set [git] push = false"
