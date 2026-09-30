"""q: the Pit CLI. The session (you, or a Claude Code agent) is the dispatcher; q only records and runs."""
import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import NAME, __version__, bag, lanes, ledger as L, book as B, spec as specmod


REPO = Path(__file__).resolve().parent.parent


def find_root() -> Path:
    """The state directory (lanes.toml, ledger/, queue/, agents/): `q --root` / $PIT_ROOT, else the nearest
    parent of the cwd holding lanes.toml and ledger/, else this checkout."""
    if os.environ.get("PIT_ROOT"):
        return Path(os.environ["PIT_ROOT"]).expanduser()
    for d in [Path.cwd(), *Path.cwd().parents]:
        if (d / "lanes.toml").exists() and (d / "ledger").is_dir():
            return d
    return REPO


def ctx():
    root = find_root()
    if not (root / "lanes.toml").exists():
        sys.exit(f"no lanes.toml in {root}: set PIT_ROOT (or q --root) to a state directory, "
                 f"or copy lanes.example.toml there")
    lg = L.Ledger(root / "ledger", os.environ.get("PIT_HOST"))
    return root, lg, lanes.load(root)


def sync(root, lg, msg):
    L.commit(root, lg, msg)
    L.push(root) or (L.git(root, "pull", "-q", "--rebase"), L.push(root))


def cmd_add(a):
    root, lg, cfg = ctx()
    ids = add_specs(root, lg, cfg, [(path, specmod.load(path)) for path in a.spec])
    sync(root, lg, f"add {', '.join(ids)}")


def add_specs(root, lg, cfg, loaded):
    st = L.fold(lg.rows())
    specs, bad, known = [], [], specmod.scenarios(root, cfg)
    for path, s in loaded:        # validate all first: a batch goes in whole or not at all
        errs = specmod.validate(s, cfg["lanes"], known, cfg.get("bench", {}).get("drivers"))
        if s.get("id") in st.jobs or s.get("id") in [x["id"] for _, x in specs]:
            errs.append(f"{s['id']} is already in the ledger (cancel or supersede it)")
        (bad.append(f"refused {path}:\n  " + "\n  ".join(errs)) if errs else specs.append((path, s)))
    if bad:
        sys.exit("\n".join(bad))
    for path, s in specs:
        if not Path(path).resolve().is_relative_to((root / "queue").resolve()):
            (root / "queue").mkdir(exist_ok=True)
            shutil.copy(path, root / "queue" / f"{s['id']}.toml")
        lg.append({"t": "node", "kind": "job", "id": s["id"], "spec": s})
        print(f"added {s['id']} ({s['lane']}, funded ${s['budget_usd']} ({specmod.funded_seconds(s, cfg['lanes'])}s), value {s['value']})")
    return [s["id"] for _, s in specs]


def cmd_post(a):
    """q add + the proposer pays the budget + an automatic stake on its `expect`, per variant."""
    root, lg, cfg = ctx()
    book, s = B.Book(lg.rows()), specmod.load(a.spec)
    mode = B.funding(book, s, a.agent, a.seed)
    err = B.check_post(book, cfg, s, a.agent, lg.rows()) if mode == "agent" else None
    if err:
        sys.exit(f"refused {a.spec}: {err}")
    add_specs(root, lg, cfg, [(a.spec, {**s, "proposer": a.agent, **({"seed": True} if mode == "seed" else {})})])
    for row in B.stakes(lg.rows(), cfg, s, a.agent, mode, a.stake):
        lg.append(row)
    sync(root, lg, f"post {s['id']} as {a.agent}")
    book = B.Book(lg.rows())
    t = {v: book.totals(s["id"], v) for v in B.variants(s)}
    print(f"{s['id']}: {mode}-funded, book " + ", ".join(f"{v} PASS ${x['pass']:.2f} / FAIL ${x['fail']:.2f}" for v, x in t.items()))
    who = B.HOUSE if mode == "seed" else a.agent
    print(f"{who} balance ${book.flows.get(who, 0.0) if who in (B.HOUSE, B.HUMAN) else book.balance(who):.2f}")


def cmd_bet(a):
    root, lg, cfg = ctx()
    rows = lg.rows()
    if len(a.args) not in (2, 3):
        sys.exit("q bet <job> [<variant>] PASS|FAIL <amount> --as <agent>")
    variant, side, amount = (["main"] + a.args)[-3:]
    amount = float(amount.lstrip("$"))
    row = lg.append(B.bet_row(B.Book(rows), L.fold(rows), a.job, variant, side.lower(), amount, a.agent))
    sync(root, lg, f"bet {a.job}/{variant} {side.upper()} as {a.agent}")
    t = B.Book(lg.rows()).totals(a.job, variant)
    print(f"{a.agent}: {side.upper()} ${amount:.2f} on {a.job}/{variant}{' (self)' if 'self' in row['tags'] else ''}"
          f" · book PASS ${t['pass']:.2f} / FAIL ${t['fail']:.2f}")


def cmd_agent(a):
    root, lg, cfg = ctx()
    book = B.Book(lg.rows())
    if a.verb == "retire":
        if not a.reason:
            sys.exit("q agent retire <id> --reason '...'")
        lg.append(B.retire_row(book, a.id, a.reason, a.by))
        sync(root, lg, f"retire {a.id}")
        return print(f"agent {a.id} retired: {a.reason}")
    if a.verb == "set":
        if not (a.runtime or a.model):
            sys.exit("q agent set <id> [--runtime claude|codex] [--model <name>]")
        row = lg.append(B.agent_set_row(book, a.id, a.runtime, a.model))
    else:
        if not a.brief:
            sys.exit("q agent add <id> --brief '<a capability or research goal to prove or refute>'")
        row = lg.append({**B.agent_row(book, a.id, a.brief, a.parent, a.runtime, a.model), **({"by": a.by} if a.by else {})})
    (root / "agents").mkdir(exist_ok=True)
    (root / "agents" / f"{a.id}.toml").write_text(
        f'id = "{a.id}"\nkind = "{row["kind"]}"\n' + (f'parent = "{row["parent"]}"\n' if row.get("parent") else "")
        + f"brief = {json.dumps(row['brief'])}\n" + "".join(f"{k} = {json.dumps(row[k])}\n" for k in ("runtime", "model") if row.get(k)))
    L.git(root, "add", str(root / "agents" / f"{a.id}.toml"))
    sync(root, lg, f"agent {a.verb} {a.id}")
    print(f"agent {a.id} ({row['kind']}; {row.get('runtime', 'claude')}{'/' + row['model'] if row.get('model') else ''})"
          + (f", planted by {a.by}" if a.by else ""))


def print_balances(book):
    for w, usd in book.balances().items():
        print(f"{w:<14} ${usd:>9.2f}")


def cmd_tick(a):
    root, lg, cfg = ctx()
    row = B.tick(lg, cfg, since=a.since)
    woke = B.wake(lg)
    if row or woke:
        sync(root, lg, f"tick {row['minutes'] if row else 0} min" + (f", wake {', '.join(w['agent'] for w in woke)}" if woke else ""))
    if row:
        print(f"drip: {row['minutes']} min, ${row['usd']:.2f} to {', '.join(row['to']) or 'nobody'}")
    print_balances(B.Book(lg.rows()))
    for w in woke:
        print(f"wake: {w['agent']} ({w['reason']}): spawn it with `q thread {w['agent']}`")


def cmd_sleep(a):
    root, lg, cfg = ctx()
    until = {"event": a.until_event or None, "balance": a.until_balance, "result": a.until_result, "market": a.until_market, "minutes": a.minutes}
    row = lg.append(B.sleep_row(B.Book(lg.rows()), a.agent, until, a.note))
    sync(root, lg, f"sleep {a.agent}")
    print(f"{a.agent} sleeps until {B.until_text(row['until'])}")


def cmd_balance(a):
    book = B.Book(ctx()[1].rows())
    if a.agent:
        print(f"{a.agent} ${book.balance(a.agent):.2f}")
    else:
        print_balances(book)


def cmd_thread(a):
    print(B.thread(ctx()[1].rows(), a.agent))


def cmd_board(a):
    root, lg, cfg = ctx()
    print(B.board(lg.rows(), cfg))


def cmd_pit(a):
    print(B.calibration(ctx()[1].rows()))


def row_line(st, jid, cfg):
    j, s = st.jobs[jid], st.jobs[jid]["spec"]
    flag = " STALE" if jid in st.stale else ""
    v = f" {j['result']['verdict']}" if j["result"] else ""
    return f"{jid:<24} {s['lane']:<11} {j['state'] + v + flag:<18} value {s['value']:<3} " \
           f"funded ${s['budget_usd']:<6} ({specmod.funded_seconds(s, cfg['lanes']):>4}s) {s['question'][:70]}"


def cmd_list(a):
    root, lg, cfg = ctx()
    if a.scenarios:
        names = specmod.scenarios(root, cfg)
        if names is None:
            sys.exit(f"no scenario registry: set [bench] scenario_dir (default: scenarios/ in {root}) or scenario_cmd in lanes.toml")
        print("\n".join(names))
        return
    st = L.fold(lg.rows())
    ids, fallback = B.order(st, lg.rows(), cfg) if a.frontier else (list(st.jobs), set())
    ids = [i for i in ids if not a.lane or st.jobs[i]["spec"]["lane"] in (a.lane, "any")]
    if a.frontier:
        print(f"frontier ({len(ids)}), " + (f"by {'matched stakes per $ budget' if B.conf(cfg)['rank'] == 'matched_per_usd' else 'matched stakes, ties cheapest'} (Pit, rank = {B.conf(cfg)['rank']}):" if B.enabled(cfg) else
                                             "in the default order (priority, critical path, value per $):"))
    for jid in ids:
        print(row_line(st, jid, cfg) + (" [fallback]" if jid in fallback else ""))


def cmd_why(a):
    root, lg, cfg = ctx()
    st = L.fold(lg.rows())
    reasons = st.why_blocked(a.id)
    if not reasons and a.id in st.jobs:
        lane = st.jobs[a.id]["spec"]["lane"]
        busy = [r for r in st.running() if st.jobs[r]["claim"]["lane"] == lane]
        slots = cfg["lanes"].get(lane, {}).get("slots", 1)
        if lane != "any" and len(busy) >= slots:
            reasons.append(f"lane {lane} busy: {', '.join(busy)}")
        ok, why = lanes.gate_open(cfg, lane)
        if not ok:
            reasons.append(f"lane {lane}: {why}")
        c = bag.conf(cfg, lane)
        if c and c["enabled"]:
            now = datetime.now(timezone.utc)
            note = bag.capped(bag.today(lg.rows(), lane, now), c, now)
            if note:
                reasons.append(f"lane {lane}: {note.removeprefix(' · ')}")
    if not reasons and a.id in st.jobs:
        s = st.jobs[a.id]["spec"]
        if not (s.get("run") or specmod.synth(s, cfg)):
            reasons.append(f"undriven: no run or scenario; a desk job for its proposer ({s.get('proposer') or 'human'}), no lane picks it")
    print(f"{a.id}: " + ("runnable" if not reasons else "\n  ".join(["blocked", *reasons])))


def cmd_show(a):
    """A node with its lineage: what the session reads before deciding "dead end?"."""
    root, lg, cfg = ctx()
    st = L.fold(lg.rows())
    print(describe(st, a.id))
    up = st.upstream(a.id)
    if up:
        print("lineage (upstream):")
        for n in sorted(up):
            print("  " + describe(st, n))
    edges = [e for e in st.all_edges() if e["from"] in up | {a.id} or e["to"] in up | {a.id}]
    for e in edges:
        print(f"  edge {e['from']} -{e['type']}-> {e['to']}")
    spent = sum(st.spend("job").get(n, 0) for n in up | {a.id})
    print(f"spend on this lineage: ${spent:.2f}")


def describe(st, n):
    if n in st.jobs:
        j = st.jobs[n]
        res = j["result"]
        tail = f" -> {res['verdict']} ${res['cost']['usd']:.2f} {res['cost']['wall_s']:.0f}s {res.get('note', '')}{' (by ' + res['agent'] + ')' if res.get('agent') else ''}" if res else ""
        return f"job {n} [{j['state']}{' STALE' if n in st.stale else ''}] {j['spec']['question']}{tail}"
    if n in st.findings:
        f = st.findings[n]
        return f"{f['kind']} {n} [{f['status']}{' STALE' if n in st.stale else ''}] {f['text']}"
    return f"{n} (not in the ledger)"


def actor(a, lg) -> dict:
    """`--as <agent>` resolved to its wallet (subs book to the parent), as a row fragment; {} when absent."""
    if not a.agent:
        return {}
    book = B.Book(lg.rows())
    if a.agent not in book.agents:
        sys.exit(f"no agent {a.agent} (q agent add)")
    return {"agent": book.wallet(a.agent)}


def cmd_cancel(a):
    root, lg, cfg = ctx()
    lg.append({"t": "cancel", "id": a.id, "reason": a.reason, **actor(a, lg)})
    B.settle_due(lg, cfg)
    sync(root, lg, f"cancel {a.id}")
    print(f"cancelled {a.id}: {a.reason}")


def cmd_decide(a):
    root, lg, cfg = ctx()
    lg.append({"t": "decision", "finding": a.id, "changed": a.changed, "note": a.note, **actor(a, lg)})
    sync(root, lg, f"decide {a.id}")
    print(f"{a.id}: decision {'changed' if a.changed else 'unchanged'} — {a.note}")


def cmd_finding(a):
    root, lg, cfg = ctx()
    st = L.fold(lg.rows())
    who = a.source or actor(a, lg).get("agent") or "session"      # no --from: name the author, never "F:None-N"
    fid = a.id or f"F:{who}-{sum(1 for f in st.findings if f.startswith(f'F:{who}-')) + 1}"      # fold keeps no author: count by id
    lg.append({"t": "node", "kind": a.kind, "id": fid, "from": a.source, "text": a.text, **actor(a, lg)})
    if a.source:
        lg.append({"t": "edge", "type": "produces", "from": a.source, "to": fid})
    for kind in ("refutes", "refines", "supersedes"):
        for target in getattr(a, kind) or []:
            lg.append({"t": "edge", "type": kind, "from": fid, "to": target})
    B.settle_due(lg, cfg)
    sync(root, lg, f"finding {fid}")
    st = L.fold(lg.rows())
    print(f"{fid}: {a.text}")
    for target in a.refutes or []:
        stale = sorted(n for n, s in st.stale.items() if s["refuted"] == target)
        print(f"  refutes {target}; now stale: {', '.join(stale) or 'nothing'}")


def cmd_edge(a):
    root, lg, cfg = ctx()
    st = L.fold(lg.rows())
    missing = [n for n in (a.src, a.dst) if n not in st.jobs and n not in st.findings]
    if missing:
        sys.exit(f"refused edge: no such node {', '.join(missing)}")
    lg.append({"t": "edge", "type": a.type, "from": a.src, "to": a.dst})
    B.settle_due(lg, cfg)
    sync(root, lg, f"edge {a.src} {a.type} {a.dst}")


def cmd_review(a):
    root, lg, cfg = ctx()
    lg.append({"t": "review", "id": a.id, "note": a.note})
    sync(root, lg, f"review {a.id}")
    print(f"{a.id} re-admitted: {a.note}")


def cmd_result(a):
    """Record a result for work done by hand (no `run`, or run outside q)."""
    from .run import record
    root, lg, cfg = ctx()
    st = L.fold(lg.rows())
    if a.id not in st.jobs:
        sys.exit(f"no job {a.id}")
    s = st.jobs[a.id]["spec"]
    dead = [r for d in s.get("depends_on", []) if "dead branch" in (r := st.dep_reason(d) or "")]
    if dead and not a.force:
        sys.exit(f"refused: {a.id} is on a dead branch ({dead[0]}); cancel it, or --force")
    meters = {k: float(v) for k, v in (m.split("=", 1) for m in a.meter)}
    rep = {"verdict": a.verdict, "meters": meters, "wall_s": a.wall_s}
    prev = st.jobs[a.id]["result"]
    if prev and not (meters or a.wall_s):     # a verdict-only correction keeps the cost already booked
        c = prev["cost"]
        rep.update(meters=c.get("meters", {}), wall_s=c.get("wall_s", 0.0))
        a.lane = a.lane or c.get("lane")
    if a.arm:     # per-variant verdicts settle each arm's market
        rep["result"] = {"verdicts": dict(x.split("=", 1) for x in a.arm)}
    row = record(lg, cfg, s, a.lane or s["lane"], {"report": rep, "wall_s": a.wall_s, "rc": 0}, agent=actor(a, lg).get("agent"))
    sync(root, lg, f"result {a.id} {a.verdict}")
    print(f"{a.id}: {a.verdict} ${row['cost']['usd']:.2f}" + (f" (by {row['agent']})" if row.get("agent") else ""))


def cmd_run(a):
    from .run import run_job
    root, lg, cfg = ctx()
    row = run_job(root, lg, cfg, a.id, a.lane, a.force_gate, agent=a.agent)
    sync(root, lg, f"result {a.id} {row['verdict']}")
    c = row["cost"]
    print(f"{a.id}: {row['verdict'].upper()} {row['note']}\n  cost: {c['wall_s']}s · {json.dumps(c.get('meters', {}))} · "
          f"${c['usd']:.2f} on {c['lane']}")
    branch = L.fold(lg.rows()).jobs[a.id]["spec"].get(f"if_{row['verdict']}")
    if branch:
        print(f"  next ({row['verdict']}): {branch}")


def cmd_graph(a):
    root, lg, cfg = ctx()
    st = L.fold(lg.rows())
    for n in sorted(st.jobs, key=lambda j: st.jobs[j]["added"]):
        print(describe(st, n))
        for e in st.all_edges():
            if e["from"] == n and e["type"] != "depends-on":
                print(f"   └─{e['type']}─► {e['to']}")
            if e["from"] == n and e["type"] == "depends-on":
                print(f"   └─depends-on─► {e['to']}")
    for f in st.findings:
        print(describe(st, f))
        for e in st.edges:
            if e["from"] == f:
                print(f"   └─{e['type']}─► {e['to']}")


def cmd_cost(a):
    root, lg, cfg = ctx()
    st = L.fold(lg.rows())
    sp = st.spend(a.by)
    for k, v in sorted(sp.items(), key=lambda kv: -kv[1]):
        print(f"{k:<28} ${v:>9.2f}")
    changed = {d["finding"] for d in st.decisions if d["changed"]}
    print(f"{'total':<28} ${sum(sp.values()):>9.2f}  ({len(changed)} decisions changed)")


def cmd_status(a):
    from .hooks import status_block
    print(status_block(ctx()[0]))


def cmd_reflect(a):
    from . import reflect
    root, lg, cfg = ctx()
    rows = lg.rows()
    if a.record:
        n = len(reflect.since_last(rows))
        lg.append({"t": "reflect", "note": a.note or "", "rows_covered": n, **({"agent": a.agent} if a.agent else {})})
        sync(root, lg, "reflect")
        print(f"reflect recorded: {n} rows covered")
    elif a.why:
        print(reflect.why(rows, datetime.now(timezone.utc), cfg))
    else:
        print(reflect.digest(rows, a.n))


def cmd_metrics(a):
    from .metrics import metrics, table
    m = metrics(ctx()[1].rows())
    if a.id and a.id not in m:
        sys.exit(f"no node {a.id}")
    print(table(m, [a.id] if a.id else None))


def cmd_replay(a):
    from .replay import main as replay
    replay(a.dir, Path(a.dir) if (Path(a.dir) / "lanes.toml").exists() else find_root(), a.out)


def cmd_bootstrap(a):
    """Print agents/BOOTSTRAP.md; --apply a proposed edit (bootstrap_add / bootstrap_remove) and commit it;
    --settle a bootstrap job: the first newcomer registered after the job vs the one before it, over their first n posts."""
    from .autopilot import bootstrap
    root, lg, cfg = ctx()
    path = root / "agents" / "BOOTSTRAP.md"
    if a.apply:
        s = specmod.load(a.apply)
        lines = path.read_text().splitlines() if path.exists() else []
        drop = {x.strip() for x in s.get("bootstrap_remove", [])}
        lines = [l for l in lines if l.strip() not in drop]
        lines += [x for x in s.get("bootstrap_add", []) if x not in lines]
        path.parent.mkdir(exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
        L.git(root, "add", str(path))
        L.git(root, "commit", "-q", "-m", f"bootstrap: apply {s.get('id', a.apply)}", "--", str(path))
        print(f"applied {s.get('id')}: +{len(s.get('bootstrap_add', []))} -{len(drop)} lines")
    elif a.settle:
        from .run import record
        rows = lg.rows()
        st = L.fold(rows)
        if a.settle not in st.jobs:
            sys.exit(f"no job {a.settle}")
        pivot = next(i for i, r in enumerate(rows) if r["t"] == "node" and r["id"] == a.settle)
        prev = B.newcomer(rows, pivot)
        old = set(B.Book(rows[:pivot]).agents)
        nxt = next((x for x, r in B.Book(rows).agents.items() if r["kind"] == "persistent" and x not in old and x != B.REFLECT), None)
        if not prev or not nxt:
            sys.exit(f"{a.settle}: no newcomer {'before' if not prev else 'after'} it yet; wait")
        before, after = B.bootstrap_cost(rows, prev, a.n), B.bootstrap_cost(rows, nxt, a.n)
        if after["posts"] < a.n:
            sys.exit(f"{a.settle}: newcomer {nxt} has {after['posts']}/{a.n} posts; wait")
        key = lambda c: (c["invalid"], c["cancelled"])
        verdict = "pass" if key(after) < key(before) else "fail"
        rep = {"verdict": verdict, "result": {"before": before, "after": after}}
        record(lg, cfg, st.jobs[a.settle]["spec"], st.jobs[a.settle]["spec"]["lane"], {"report": rep, "wall_s": 0.0, "rc": 0}, agent=actor(a, lg).get("agent"))
        sync(root, lg, f"result {a.settle} {verdict}")
        print(f"{a.settle}: {verdict} ({prev} invalid {before['invalid']:.0%} -> {nxt} {after['invalid']:.0%})")
    else:
        print(bootstrap(root))


def cmd_autopilot(a):
    from .autopilot import Autopilot
    root, lg, cfg = ctx()
    Autopilot(root, lg, cfg, dry=a.dry_run, cap=a.max_usd_per_hour,
              sub_cap=a.max_subagent_runs_per_hour, for_=a.for_).loop(a.interval, a.once)


def cmd_view(a):
    from .view import serve
    root, lg, cfg = ctx()
    serve(lg.rows, cfg, a.port, not a.no_open, host=a.host)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["runner"]:          # the runner needs no state directory: it only runs what it is sent
        from . import runner_service
        return runner_service.main(argv[1:])
    ap = argparse.ArgumentParser(prog="q", description=f"{NAME}: a prediction market that schedules experiments, over a ledger of jobs and findings. You dispatch.")
    ap.add_argument("--version", action="version", version=f"{NAME.lower()} {__version__}")
    ap.add_argument("--root", help="the state directory (default: $PIT_ROOT, else the nearest one above the cwd, else this checkout)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("add", help="validate specs and add them"); p.add_argument("spec", nargs="+"); p.set_defaults(f=cmd_add)
    p = sub.add_parser("list"); p.add_argument("--lane"); p.add_argument("--frontier", action="store_true")
    p.add_argument("--scenarios", action="store_true", help="the scenario names a job may run, one per line"); p.set_defaults(f=cmd_list)
    for name in ("why-blocked", "why"):
        p = sub.add_parser(name); p.add_argument("id"); p.set_defaults(f=cmd_why)
    p = sub.add_parser("show", help="a node and its lineage"); p.add_argument("id"); p.set_defaults(f=cmd_show)
    p = sub.add_parser("cancel"); p.add_argument("id"); p.add_argument("--reason", required=True); p.add_argument("--as", dest="agent"); p.set_defaults(f=cmd_cancel)
    p = sub.add_parser("decide"); p.add_argument("id")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--changed", action="store_true"); g.add_argument("--unchanged", dest="changed", action="store_false")
    p.add_argument("--note", required=True); p.add_argument("--as", dest="agent"); p.set_defaults(f=cmd_decide)
    p = sub.add_parser("finding"); p.add_argument("--from", dest="source"); p.add_argument("--text", required=True)
    p.add_argument("--id"); p.add_argument("--kind", default="finding", choices=("finding", "hypothesis"))
    p.add_argument("--refutes", action="append"); p.add_argument("--refines", action="append")
    p.add_argument("--supersedes", action="append"); p.add_argument("--as", dest="agent", help="the agent recording it")
    p.set_defaults(f=cmd_finding)
    p = sub.add_parser("edge"); p.add_argument("src"); p.add_argument("type", choices=L.EDGE_TYPES); p.add_argument("dst"); p.set_defaults(f=cmd_edge)
    p = sub.add_parser("review", help="re-admit a stale node"); p.add_argument("id"); p.add_argument("--note", required=True); p.set_defaults(f=cmd_review)
    p = sub.add_parser("result", help="record a hand-run result"); p.add_argument("id")
    p.add_argument("--verdict", required=True, choices=specmod.VERDICTS); p.add_argument("--wall-s", type=float, default=0.0)
    p.add_argument("--meter", action="append", default=[], help="NAME=N, e.g. tok_in=1200 (priced by the lane)")
    p.add_argument("--lane"); p.add_argument("--as", dest="agent", help="the agent recording it")
    p.add_argument("--force", action="store_true", help="record even on a dead @pass/@fail branch")
    p.add_argument("--arm", action="append", help="VARIANT=pass|fail|invalid, per arm"); p.set_defaults(f=cmd_result)
    p = sub.add_parser("run", help="run a job's command: claim, kill when the funding runs out, stop rules, result")
    p.add_argument("id"); p.add_argument("--lane"); p.add_argument("--force-gate", action="store_true")
    p.add_argument("--as", dest="agent", help="the agent claiming it"); p.set_defaults(f=cmd_run)
    p = sub.add_parser("post", help="add a spec as an agent: it pays the budget and stakes its expect")
    p.add_argument("spec"); p.add_argument("--as", dest="agent", required=True, help="an agent, `reflect` or `human`")
    p.add_argument("--seed", action="store_true", help="a root the house stakes from its vig pool")
    p.add_argument("--stake", type=float, default=0.0, help="--as human: the stake per variant on its expect")
    p.set_defaults(f=cmd_post)
    p = sub.add_parser("sleep", help="sleep until a condition; q tick wakes the agent")
    p.add_argument("--as", dest="agent", required=True)
    p.add_argument("--until-event", action="store_true", help="wake on the next board event (autopilot's default for every agent)")
    p.add_argument("--until-balance", type=float)
    p.add_argument("--until-result"); p.add_argument("--until-market"); p.add_argument("--minutes", type=float)
    p.add_argument("--note", default=""); p.set_defaults(f=cmd_sleep)
    p = sub.add_parser("bet", help="q bet <job> [<variant>] PASS|FAIL <amount> --as <agent>")
    p.add_argument("job"); p.add_argument("args", nargs="+"); p.add_argument("--as", dest="agent", required=True)
    p.set_defaults(f=cmd_bet)
    p = sub.add_parser("agent", help="q agent add <id> --brief ... [--parent <id>] [--runtime claude|codex] [--model <name>] | "
                                     "q agent set <id> --runtime ... --model ... | q agent retire <id> --reason ...")
    p.add_argument("verb", choices=("add", "set", "retire")); p.add_argument("id"); p.add_argument("--brief"); p.add_argument("--parent")
    p.add_argument("--runtime", help="what its turns run on: claude (default) or codex"); p.add_argument("--model", help="default: [autopilot] runtimes.<runtime>.model")
    p.add_argument("--reason"); p.add_argument("--as", dest="by", help="who adds or retires it (reflect, when it plants or retires)")
    p.set_defaults(f=cmd_agent)
    p = sub.add_parser("tick", help="pay the income since the last tick; print balances")
    p.add_argument("--since", help="ISO time the first tick counts from"); p.set_defaults(f=cmd_tick)
    p = sub.add_parser("balance"); p.add_argument("--as", dest="agent"); p.set_defaults(f=cmd_balance)
    p = sub.add_parser("thread", help="an agent's line: brief, balance, nodes, open bets, open markets")
    p.add_argument("agent"); p.set_defaults(f=cmd_thread)
    p = sub.add_parser("board", help="every open market with its price, unopposed first (the board agents see)")
    p.set_defaults(f=cmd_board)
    p = sub.add_parser("pit", help="q pit calibration"); p.add_argument("what", choices=("calibration",)); p.set_defaults(f=cmd_pit)
    p = sub.add_parser("graph"); p.set_defaults(f=cmd_graph)
    p = sub.add_parser("cost"); p.add_argument("--by", choices=("lane", "job"), default="lane"); p.set_defaults(f=cmd_cost)
    p = sub.add_parser("status", help="the SessionStart block"); p.set_defaults(f=cmd_status)
    p = sub.add_parser("reflect", help="digest of the ledger since the last reflection; --record restarts the counter")
    p.add_argument("--since-last", action="store_true"); p.add_argument("--n", type=int)
    p.add_argument("--record", action="store_true"); p.add_argument("--note"); p.add_argument("--why", action="store_true")
    p.add_argument("--as", dest="agent", help="the reflecting identity (reflect, or a sub of it)")
    p.set_defaults(f=cmd_reflect)
    p = sub.add_parser("bootstrap", help="agents/BOOTSTRAP.md; --apply <proposed.toml>; --settle <job>")
    p.add_argument("--apply"); p.add_argument("--settle"); p.add_argument("-n", type=int, default=20)
    p.add_argument("--as", dest="agent", default="reflect"); p.set_defaults(f=cmd_bootstrap)
    p = sub.add_parser("metrics", help="derived per-node numbers: cost, budget ratios, lineage spend, depth"); p.add_argument("id", nargs="?")
    p.set_defaults(f=cmd_metrics)
    p = sub.add_parser("autopilot", help="run the loop: dispatch per free lane, hand results back to agents, reflect")
    p.add_argument("--once", action="store_true"); p.add_argument("--interval", type=float, default=60)
    p.add_argument("--max-usd-per-hour", type=float, help="spend gate; default: [autopilot] max_usd_per_hour (unset/0 = no gate)")
    p.add_argument("--max-subagent-runs-per-hour", type=int, help="turn budget, reported not enforced; default: [autopilot] max_subagent_runs_per_hour (60)")
    p.add_argument("--for", dest="for_", help="stop the loop after this wall time: 1h, 45m, 2h30m, or seconds")
    p.add_argument("--dry-run", action="store_true", help="print the plan; write, claim, run and spawn nothing")
    p.set_defaults(f=cmd_autopilot)
    p = sub.add_parser("view", help="serve the terminal (markets, lanes, agents, tape) at http://127.0.0.1:8790/"); p.add_argument("--port", type=int, default=8790); p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-open", action="store_true"); p.set_defaults(f=cmd_view)
    sub.add_parser("runner", help="serve runs on this box over HTTP (docs/runner.md); q runner --help", add_help=False)
    p = sub.add_parser("replay"); p.add_argument("dir"); p.add_argument("--out", help="write the simulated ledger here (ndjson)")
    p.set_defaults(f=cmd_replay)
    a = ap.parse_args(argv)
    if a.root:
        os.environ["PIT_ROOT"] = a.root     # one resolution path for q, its subprocesses and gates
    a.f(a)


if __name__ == "__main__":
    main()
