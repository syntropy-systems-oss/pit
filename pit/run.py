"""`q run <id>`: run a job's command until its funding runs out, enforce the stop rules, ledger the result.

A command reports its verdict and cost by printing one line (anywhere in its output; the last one wins per key):
    pit: verdict=pass wall_s=74.7 meters={"tok_in": 1200, "tok_out": 900, "tok_cached": 48000} result={"top3": ["a","b","c"]}
Every key is optional; `meters=` is a JSON object of named counts (priced by the lane, see lanes.cost_line) and
`result=` must come last (the rest of the line is JSON). Without a verdict the run books INVALID.

A lane with `url` runs the job on a runner service (pit/runner_service.py, docs/runner.md) instead of a local fork:
the command is POSTed to <url>/run and the streamed reply is read exactly like a forked process's output.
"""
import json
import os
import re
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request

from . import lanes, ledger as L, book as B, spec as specmod

LINE = re.compile(r"^pit:\s+(.*)$")
STOPS = {
    "feedback_report": re.compile(r"feedback[_:]report"),
    "cache_miss": re.compile(r"prompt cache miss|cache_miss\b"),
}


def parse_line(line: str) -> dict | None:
    m = LINE.match(line.strip())
    if not m:
        return None
    body, _, result = m[1].partition("result=")
    out = {}
    if "meters=" in body:
        pre, _, rest = body.partition("meters=")
        out["meters"], end = json.JSONDecoder().raw_decode(rest)
        body = pre + rest[end:]
    out.update(kv.split("=", 1) for kv in body.split() if "=" in kv)
    if "wall_s" in out:
        out["wall_s"] = float(out["wall_s"])
    if result.strip():
        out["result"] = json.loads(result)
    return out


def _read(lines, fail_on: list[str], stop, echo) -> tuple[list[str], dict, dict]:
    """Consume output lines: echo them, merge the `pit:` reports, call stop() on the first stop-rule hit."""
    out, hit, report = [], {}, {}
    for line in lines:
        out.append(line)
        echo("  | " + line.rstrip())   # prefixed, so a hook never double-ledgers the child's pit: line
        rep = parse_line(line)
        if rep:
            report.update(rep)
        for name in fail_on:
            if name in STOPS and STOPS[name].search(line) and not hit:
                hit.update(stop=name, stop_text=line.strip())
                stop()
    return out, hit, report


def execute(cmd: str, timeout_s: float, fail_on: list[str], cwd=None, echo=print, env=None) -> dict:
    """Run `cmd` with sh; kill on a stop signal or at timeout_s. Returns {rc, wall_s, stop, stop_text, report, output}."""
    t0 = time.monotonic()
    p = subprocess.Popen(["sh", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         cwd=cwd, start_new_session=True, env={**os.environ, **(env or {})})
    res = {}
    th = threading.Thread(target=lambda: res.update(zip(("out", "hit", "report"), _read(p.stdout, fail_on, lambda: _kill(p), echo))), daemon=True)
    th.start()
    timeout = {}
    try:
        p.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timeout = {"stop": "timeout", "stop_text": f"killed at {timeout_s:.0f}s: what the funding buys"}
        _kill(p)
        p.wait()
    wall = time.monotonic() - t0     # before the join: a grandchild that outlives the kill holds the pipe open and would bill its sleep
    th.join(timeout=5)
    p.stdout.close()
    return {"rc": p.returncode, "wall_s": wall, **timeout, **res.get("hit", {}), "report": res.get("report", {}),
            "output": "".join(res.get("out", []))}


def execute_remote(url: str, body: dict, fail_on: list[str], echo=print) -> dict:
    """POST the job to a runner service and read its streamed reply like a fork's output. The runner kills the run at
    body["funded_s"] and ends with `pit: [stop=timeout] wall_s=S` (S = the whole request, fetch and build included)."""
    t0 = time.monotonic()
    tok = os.environ.get("PIT_RUNNER_TOKEN")
    req = urllib.request.Request(url.rstrip("/") + "/run", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {tok}"} if tok else {})})
    try:
        resp = urllib.request.urlopen(req, timeout=body["funded_s"] + 120)
    except (urllib.error.URLError, OSError) as e:
        why = e.read().decode(errors="replace").strip() if isinstance(e, urllib.error.HTTPError) else str(e)
        return {"rc": None, "wall_s": time.monotonic() - t0, "output": why,
                "report": {"verdict": "invalid", "note": f"runner {url}: {why}"}}
    with resp:
        out, hit, report = _read(_lines(resp), fail_on, resp.close, echo)
    if report.pop("stop", None) == "timeout":
        hit.setdefault("stop", "timeout")
        hit.setdefault("stop_text", f"the runner killed it at {body['funded_s']}s: what the funding buys")
    return {"rc": 0, "wall_s": time.monotonic() - t0, **hit, "report": report, "output": "".join(out)}


def _lines(resp):
    try:
        for b in resp:
            yield b.decode(errors="replace")
    except (ValueError, OSError):       # closed by a stop rule, or the runner went away: what arrived is the trace
        return


def _kill(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def verdict_of(run: dict) -> tuple[str, str]:
    """(verdict, note) from the stop rules first, then the reported verdict, then the exit code."""
    stop = run.get("stop")
    if stop == "timeout":
        # over budget is a FAIL, not a void: the partial trace is kept for the proposer, PASS stakes lose,
        # so underbidding time to jump the queue costs the bidder
        return "fail", f"over budget: {run['stop_text']}; partial trace kept"
    if stop:
        return "fail", f"{stop}: {run['stop_text']}"
    v = run["report"].get("verdict")
    if v in specmod.VERDICTS:
        # a reported invalid must name its cause: the driver's own note, else the counts it sent
        if v == "invalid":
            return v, run["report"].get("note") or f"driver reported invalid: {run['report'].get('result', {})}"
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
    synth = specmod.synth(s, cfg)
    if not s.get("run") and not synth:
        raise SystemExit(f"{jid} has no run command: do it by hand, then `q result {jid} --verdict ...`")
    inputs, cmd = specmod.render({**s, "run": s.get("run") or synth[0]}, st.render_ctx())
    extra = {"agent": agent} if agent else {}
    if synth:
        extra["run"] = cmd                # the harness-supplied driver, on the claim row (and so the tape)
    if B.enabled(cfg) and jid in B.order(st, ledger.rows(), cfg)[1]:
        extra["fallback"] = True          # the stall fallback: exploration spend, logged on the claim
    push_ok = bool(cfg.get("git", {}).get("push", False))     # opt-in: private state is never pushed by accident
    if not push_ok or not L.has_remote(root):
        echo("no remote: local lock" if not L.has_remote(root) else "[git] push off: local lock")
    won, cid = L.claim(root, ledger, jid, lane, extra=extra, push_ok=push_ok)
    if not won:
        raise SystemExit(cid)
    echo(f"q run {jid} on {lane}: {cmd}")
    funded, l = specmod.funded_seconds(s, cfg["lanes"]), cfg["lanes"].get(lane, {})
    env = {"PIT_JOB": jid, "PIT_LANE": lane, "PIT_FUNDED_S": str(funded)}
    if l.get("url"):
        where = {"repo": l["repo"], "ref": s.get("ref") or l.get("ref", "HEAD"), "script": cmd} if l.get("repo") else {"command": cmd}
        r = execute_remote(l["url"], {"job": jid, **where, "funded_s": funded, "env": env}, s["fail_on"], echo=echo)
    else:
        r = execute(cmd, funded, s["fail_on"], cwd=os.path.expanduser(s["cwd"]) if s.get("cwd") else (root if synth else None),
                    echo=echo, env=env)
    return record(ledger, cfg, s, lane, r)


def record(ledger: L.Ledger, cfg: dict, s: dict, lane: str, r: dict, ts: str | None = None, agent: str | None = None) -> dict:
    """Book a run: its cost line, a FAIL if the cost passed the funding, and the funding row (unspent back, overage taken)."""
    verdict, note = verdict_of(r)
    rep = r["report"]
    cost = lanes.cost_line(cfg, rep.get("lane", lane), rep.get("wall_s", r["wall_s"]), rep.get("meters"))
    budget = s.get("budget_usd", 0)
    if cost["usd"] > budget:
        verdict, note = "fail", (f"over budget: ${cost['usd']:.2f} of ${budget:.2f} (time ${cost['usd_time']:.2f}, "
                                 f"meters ${cost['usd'] - cost['usd_time']:.2f}); trace kept")
    rows = ledger.rows()
    fund = B.funding_row(rows, s, cost["usd"])
    if fund.get("shortfall"):
        note += f"; {fund['wallet']} short ${fund['shortfall']:.2f} of the overage"
    row = ledger.append({"t": "result", "job": s["id"], "verdict": verdict, "cost": cost,
                         "result": rep.get("result", {}), "note": note, **({"funding": fund} if fund else {}),
                         **({"agent": agent} if agent else {})}, ts)
    L.settle(ledger, s, verdict, ts)
    B.settle_due(ledger, cfg)
    return row
