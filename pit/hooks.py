"""Claude Code hook entry points: `python -m pit.hooks session-start|stop|post-tool-use` (stdin = hook JSON).

- session-start: the frontier, today's spend by lane, and what a refutation made stale (<= 20 lines, to stdout,
  which Claude Code adds to the session's context). Saves a watermark for this session.
- stop: rows appended since the watermark whose taken branch names a human decision, or dead ends -> notify.sh.
- post-tool-use: a tool output line starting `pit: job=<id> ...` becomes a result row.
"""
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import NAME, bag, lanes, ledger as L, book as B, reflect, spec as specmod

HUMAN = re.compile(r"\b(human|owner|approv\w*|decide|decision|ask)\b", re.I)
MAX_LINES = 20


def status_block(root, today: str | None = None) -> str:
    root = Path(root)
    cfg = lanes.load(root)
    st = L.fold(L.Ledger(root / "ledger").rows())
    today = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    front = B.order(st, L.Ledger(root / "ledger").rows(), cfg)[0]
    lines = [f"{NAME}: {len(front)} runnable, {len(st.running())} running, {len(st.stale)} stale "
             f"({sum(1 for j in st.jobs.values() if j['state'] == 'queued')} queued). You pick; `q run <id>` runs one."]
    for jid in front[:7]:
        s = st.jobs[jid]["spec"]
        tag = "ready" if s.get("run") or specmod.synth(s, cfg) else "desk "     # desk = no run/scenario: its proposer does it by hand
        lines.append(f"  {tag} {jid} [{s['lane']}, {s['budget_s']}s, ${s['budget_usd']}] {s['question'][:80]}")
    if len(front) > 7:
        lines.append(f"  ... {len(front) - 7} more: q list --frontier")
    for jid in st.running()[:3]:
        lines.append(f"  running {jid} on {st.jobs[jid]['claim']['lane']} since {st.jobs[jid]['claim']['ts'][11:16]}Z")
    spend: dict[str, float] = {}
    for r in st.results:
        if r["ts"].startswith(today):
            spend[r["cost"]["lane"]] = spend.get(r["cost"]["lane"], 0) + r["cost"]["usd"]
    for n in cfg["lanes"]:
        c = bag.conf(cfg, n)
        if c and c["enabled"]:
            now = datetime.now(timezone.utc)
            k = bag.today(L.Ledger(root / 'ledger').rows(), n, now)
            lines.append(f"  {n} bag: {k}/{c['max_per_day']} today{bag.capped(k, c, now)}")
    lines.append("  spend today: " + (", ".join(f"{k} ${v:.2f}" for k, v in sorted(spend.items())) or "$0"))
    rows = L.Ledger(root / "ledger").rows()
    since, why = reflect.since_last(rows), reflect.due(rows, datetime.now(timezone.utc), cfg)
    lines.append(f"  {len(since)} rows since last reflection" + (f"; reflect due ({why}): /pit:reflect" if why else ""))
    stale = sorted(st.stale.items(), key=lambda kv: kv[1]["ts"])
    room = MAX_LINES - len(lines) - 1
    for n, s in stale[:room]:
        lines.append(f"  STALE {n}: {s['refuted']} refuted by {s['by']} at {s['ts'][11:16]}Z (q review {n} --note ...)")
    if len(stale) > room:
        lines.append(f"  ... {len(stale) - room} more stale: q list")
    return "\n".join(lines)


def _mark(root, sid: str) -> Path:
    d = Path(root) / ".pit" / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{re.sub(r'[^A-Za-z0-9_-]', '_', sid or 'default')}.json"


def session_start(root, hook: dict) -> str:
    rows = L.Ledger(Path(root) / "ledger").rows()
    _mark(root, hook.get("session_id", "")).write_text(json.dumps({"ts": rows[-1]["ts"] if rows else ""}))
    return status_block(root)


def news_since(root, since: str) -> list[str]:
    """One-line items worth a ping: results whose branch names a human decision, and dead ends."""
    rows = L.Ledger(Path(root) / "ledger").rows()
    st = L.fold(rows)
    items = []
    for r in rows:
        if r["ts"] <= since:
            continue
        if r["t"] == "result" and r["job"] in st.jobs:
            branch = st.jobs[r["job"]]["spec"].get(f"if_{r['verdict']}", "")
            if HUMAN.search(branch):
                items.append(f"{r['job']} {r['verdict'].upper()} -> {branch[:120]}")
        if r["t"] == "cancel" and r.get("reason", "").lower().startswith("dead end"):
            items.append(f"{r['id']} dead end: {r['reason'][:120]}")
    return items


def stop(root, hook: dict) -> list[str]:
    mark = _mark(root, hook.get("session_id", ""))
    since = json.loads(mark.read_text())["ts"] if mark.exists() else L.now()
    items = news_since(root, since)
    rows = L.Ledger(Path(root) / "ledger").rows()
    mark.write_text(json.dumps({"ts": rows[-1]["ts"] if rows else since}))
    if items:
        msg = f"{NAME}: " + " | ".join(items)
        subprocess.run([str(Path(__file__).resolve().parent.parent / "scripts" / "notify.sh"), msg], timeout=15)
    return items


def post_tool_use(root, hook: dict) -> list[dict]:
    from .run import parse_line, record
    resp = hook.get("tool_response", "")
    text = resp if isinstance(resp, str) else "\n".join(str(resp.get(k, "")) for k in ("stdout", "output", "content")) \
        if isinstance(resp, dict) else str(resp)
    lg = L.Ledger(Path(root) / "ledger", os.environ.get("PIT_HOST"))
    cfg = lanes.load(root)
    out = []
    for line in text.splitlines():
        rep = parse_line(line)
        if not rep or "job" not in rep:
            continue
        st = L.fold(lg.rows())
        j = st.jobs.get(rep["job"])
        if not j or j["result"]:      # unknown job, or already recorded (e.g. by `q run`)
            continue
        rep.setdefault("verdict", "unknown")
        out.append(record(lg, cfg, j["spec"], rep.get("lane", j["spec"]["lane"]),
                          {"report": rep, "wall_s": rep.get("wall_s", 0.0), "rc": 0}))
    if out:
        L.commit(root, lg, f"hook: result {', '.join(r['job'] for r in out)}")
    rows = lg.rows()
    since, why = reflect.since_last(rows), reflect.due(rows, datetime.now(timezone.utc), cfg)
    if reflect.nudge(Path(root), why, len(since)):
        print(f"{NAME}: {len(since)} rows since last reflection; reflect due ({why}): /pit:reflect")
    return out


def main(argv=None):
    from .cli import find_root
    event = (argv or sys.argv[1:])[0]
    try:
        hook = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        hook = {}
    root = find_root()
    if not (root / "lanes.toml").exists():
        return 0      # no Pit state here: stay silent
    if event == "session-start":
        print(session_start(root, hook))
    elif event == "stop":
        stop(root, hook)
    elif event == "post-tool-use":
        post_tool_use(root, hook)
    return 0


if __name__ == "__main__":
    sys.exit(main())
