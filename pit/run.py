"""`q run <id>`: run a job's shell command under its budget, enforce the stop rules, ledger the result.

A command reports cost and verdict by printing one line (anywhere in its output):
    pit: job=<id> verdict=pass uncached=1200 cached=48000 out=900 wall_s=74.7 result={"top3": ["a","b","c"]}
Every key is optional; `result=` must come last (the rest of the line is JSON). Without a verdict, exit 0 = pass.
"""
import json
import os
import re
import signal
import subprocess
import threading
import time

from . import lanes, ledger as L, book as B, spec as specmod

LINE = re.compile(r"^pit:\s+(.*)$")
STOPS = {
    "feedback_report": re.compile(r"feedback[_:]report"),
    "cache_miss": re.compile(r"prompt cache miss|cache_miss\b|cached[= ]0\b.*after call 1"),
}


def parse_line(line: str) -> dict | None:
    m = LINE.match(line.strip())
    if not m:
        return None
    body, _, result = m[1].partition("result=")
    out = dict(kv.split("=", 1) for kv in body.split() if "=" in kv)
    for k in ("uncached", "cached", "out"):
        if k in out:
            out[k] = int(float(out[k]))
    if "wall_s" in out:
        out["wall_s"] = float(out["wall_s"])
    if result.strip():
        out["result"] = json.loads(result)
    return out


def execute(cmd: str, timeout_s: float, fail_on: list[str], cwd=None, echo=print) -> dict:
    """Run `cmd` with sh; kill on a stop signal or at timeout_s. Returns {rc, wall_s, stop, stop_text, report, output}."""
    t0 = time.monotonic()
    p = subprocess.Popen(["sh", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         cwd=cwd, start_new_session=True)
    out, hit, reports = [], {}, []

    def read():
        for line in p.stdout:
            out.append(line)
            echo("  | " + line.rstrip())   # prefixed, so a hook never double-ledgers the child's pit: line
            rep = parse_line(line)
            if rep:
                reports.append(rep)
            for name in fail_on:
                if name in STOPS and STOPS[name].search(line) and not hit:
                    hit.update(stop=name, stop_text=line.strip())
                    _kill(p)

    th = threading.Thread(target=read, daemon=True)
    th.start()
    try:
        p.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        hit.setdefault("stop", "timeout")
        hit.setdefault("stop_text", f"killed at {timeout_s:.0f}s (2x budget)")
        _kill(p)
        p.wait()
    wall = time.monotonic() - t0     # before the join: a grandchild that outlives the kill holds the pipe open and would bill its sleep
    th.join(timeout=5)
    p.stdout.close()
    report = {}
    for r in reports:
        report.update(r)
    return {"rc": p.returncode, "wall_s": wall, **hit, "report": report, "output": "".join(out)}


def _kill(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def verdict_of(run: dict) -> tuple[str, str]:
    """(verdict, note) from the stop rules first, then the reported verdict, then the exit code."""
    stop = run.get("stop")
    if stop == "timeout":
        return "invalid", run["stop_text"]
    if stop:
        return "fail", f"{stop}: {run['stop_text']}"
    v = run["report"].get("verdict")
    if v in specmod.VERDICTS:
        return v, ""
    # no reported verdict is a non-run (refusal, placeholder, unparsed driver line), not evidence
    return "invalid", f"exit {run['rc']} with no verdict"


def run_job(root, ledger: L.Ledger, cfg: dict, jid: str, lane: str | None = None, force_gate=False,
            echo=print, agent: str | None = None) -> dict:
    st = L.fold(ledger.rows())
    blocked = st.why_blocked(jid)
    if blocked:
        raise SystemExit(f"{jid} is not runnable: " + "; ".join(blocked))
    s = st.jobs[jid]["spec"]
    lane = lane or s["lane"]
    if s["lane"] != "any" and lane != s["lane"]:
        raise SystemExit(f"{jid} runs on {s['lane']}, not {lane}")
    ok, why = lanes.gate_open(cfg, lane)
    if not ok and not force_gate:
        raise SystemExit(f"lane {lane}: {why}")
    if not s.get("run"):
        raise SystemExit(f"{jid} has no run command: do it by hand, then `q result {jid} --verdict ...`")
    inputs, cmd = specmod.render(s, st.render_ctx())
    extra = {"agent": agent} if agent else {}
    if B.enabled(cfg) and jid in B.order(st, ledger.rows(), cfg)[1]:
        extra["fallback"] = True          # the stall fallback: exploration spend, logged on the claim
    won, cid = L.claim(root, ledger, jid, lane, extra=extra)
    if not won:
        raise SystemExit(cid)
    echo(f"q run {jid} on {lane}: {cmd}")
    r = execute(cmd, 2 * s["budget_s"], s["fail_on"], cwd=s.get("cwd") and os.path.expanduser(s["cwd"]), echo=echo)
    return record(ledger, cfg, s, lane, r)


def record(ledger: L.Ledger, cfg: dict, s: dict, lane: str, r: dict, ts: str | None = None) -> dict:
    verdict, note = verdict_of(r)
    rep = r["report"]
    cost = lanes.cost_line(cfg, rep.get("lane", lane), rep.get("uncached", 0), rep.get("cached", 0),
                           rep.get("out", 0), rep.get("wall_s", r["wall_s"]))
    row = ledger.append({"t": "result", "job": s["id"], "verdict": verdict, "cost": cost,
                         "result": rep.get("result", {}), "note": note}, ts)
    L.settle(ledger, s, verdict, ts)
    B.settle_due(ledger, cfg)
    return row
