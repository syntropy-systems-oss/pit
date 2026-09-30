"""`q autopilot`: the loop that runs Pit between sessions (a state machine over the ledger).

Each tick: STOP file? -> B.tick (drip + wakes) -> per lane: free? pick B.order()[0] among jobs with a `run`,
gate -> claim+run in a subprocess (`q run`, so lanes run at once; `any` gets a small worker pool) ->
desk jobs (no `run`) become a `wake` for their proposer -> hand-backs: every result of a posted job and every
wake spawns the proposer as a subagent on its runtime (claude or codex; a sub `<agent>-<ts>`, so it books to the parent) ->
reflection when reflect.due() fires (Opus by default, a sub of `reflect`; proposals are printed and the session notified, never added).
Every decision is an `auto` row {type, lane, job, agent, reason}; fold() and Book ignore them.
All state is the ledger plus the child processes of this loop, so a restart resumes where it left off.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import bag, lanes, ledger as L, book as B, reflect, spec as specmod

REPO = Path(__file__).resolve().parent.parent
RULES = ("Money is a scheduling signal, not real. Never spend real money, never send messages to people, never touch "
         "production; bench runs use mocks only; nothing over an hour.")
CLAIM = ("Your brief is a capability or research goal to prove or refute; it is your goal. Test it where it could fail, "
         "including held-out variants with the step-by-step instructions that make it work removed. This turn: (1) state the claim in one line and the "
         "strongest evidence for and against it from your thread. (2) Your next experiment: name the cheapest run "
         "that could change your mind and post it (`q post --as <you>` — you pay; if you cannot afford the lane you "
         "want, post it on a cheaper lane or `q sleep --until-balance` naming the run). Posting is the primary action "
         "every turn; or record a finding if your evidence already settles something. If a result of yours settles a question by "
         "analysis and implies a concrete change or run, post that change as your next job before you sleep. "
         "An experiment is a runnable bench job: a `run` command, or `scenario = \"<name>\"` on a lane with a runner "
         "(`q list --scenarios` names them). A desk job with no run is not an experiment and does not count. You are never "
         "idle: while a run of yours is in flight, bet, research or post a second experiment on a free lane; propose new "
         "cases the system could plausibly get wrong rather than re-auditing the machine.")
DESK = ("This desk job is yours and stays on your plate until it has a result. Do it now and record the result with "
        "`q result <job> --verdict … --as <you>` citing the commit or file you produced; or, if it is too big for one turn, "
        "post ONE narrower job that gets it started (`q post --as <you>`) and record this one as invalid with a note; or "
        "cancel it with a reason (`q cancel <job> --reason … --as <you>`). Do not leave it queued.")
BOARD = ("The board is context. If an open run bears on your claim you may bet on it (that is how you get paid for "
         "understanding what others are finding), and you may bet where you have a reason even if it does not. The market "
         "is not the goal: it buys you time on the machines and pays you for understanding. You are paid for understanding "
         "only when you take the other side of someone's stake; a post alone only spends. Unopposed markets are listed "
         "first: if you believe the proposer is wrong, $1 there is the cheapest bet on the board.")
REWAKE = ("Agents never sleep: you get a turn about every {gap} s whether or not a run of yours is in flight. While one is, "
          "use the turn to bet on other open runs, research, or post a second experiment on a free lane. "
          "Every turn must leave the market changed: a post, a bet, or a finding.")
BOOTSTRAP_LOOP = ("You maintain agents/BOOTSTRAP.md. If the tape shows a newcomer paying for something the text does not say, "
                  "propose the edit as a job spec in queue/proposed/ (lane any, no run): `question` = the exact line(s) to add or "
                  "change, `expect = pass`, `if_pass` = 'the next newcomer's bootstrap cost over its first 20 posts is lower than "
                  "the previous newcomer's', `if_fail` = 'revert the line'; carry the edit as `bootstrap_add = [\"<line>\", ...]` and "
                  "`bootstrap_remove = [\"<exact existing line>\", ...]`. The session applies accepted edits (`q bootstrap --apply`) "
                  "and records the result (`q bootstrap --settle <job>`) when the next newcomer's first 20 posts are in.")
STRUCTURE = ("You are the one agent with a standing, structural brief: the shape of the population. Every other agent's brief is a "
             "specific, falsifiable capability or research goal, never a role. Look across all agents' findings and results for "
             "struggles several of them share; for a shared cause, plant a new persistent agent whose brief names the capability "
             "that would remove it (`q agent add <id> --brief '<capability>' --as reflect`) and post its first experiment as a root "
             "(`q post <spec> --as reflect`: the house seeds it from the vig pool). Retire an agent whose capability is proven, or "
             "whose brief has stopped producing new evidence (`q agent retire <id> --reason '<why>' --as reflect`): it stays on the "
             "book and gets no more wakes or income. When you see a brief narrowing into step-by-step instructions to the runtime, "
             "plant an agent whose goal is that the capability holds without those instructions (held-out variants with the "
             "instructions removed). Write these as proposals in queue/proposed/ with the exact commands; the session runs them.")
# codex exec: no git-repo check (the state root may be any dir), commands sandboxed to the root + add_dirs with no
# network (the model call itself is outside the sandbox), never ask for approval (nobody is there to answer)
CODEX_ARGS = ["--skip-git-repo-check", "--sandbox", "workspace-write", "-c", 'approval_policy="never"']
DEFAULTS = {"max_usd_per_hour": 0,       # 0 = no spend gate (an open market); hour spend is reported only
             "any_workers": 2, "permission_mode": "", "allowed_tools": [],
            "runtimes": {}, "reflect": {},
            "max_subagent_runs_per_hour": 60, "heartbeat_minutes": 20, "heartbeat_min_usd": 1.0, "idle_wake_gap_s": 90}


def conf(cfg: dict) -> dict:
    return {**DEFAULTS, **cfg.get("autopilot", {})}


def hour_spend(rows: list[dict], now: datetime) -> float:
    cut = B.iso(now - timedelta(hours=1))
    last = {r["job"]: r for r in rows if r["t"] == "result"}      # a correction replaces the row it corrects, it does not add to it
    return round(sum(r["cost"]["usd"] for r in last.values() if r["ts"] >= cut), 4)


def subagent_runs(rows: list[dict], now: datetime) -> int:
    """Real-token spawns (hand-backs, event wakes + reflections) in the last hour."""
    cut = B.iso(now - timedelta(hours=1))
    return sum(1 for r in rows if r["t"] == "auto" and r["ts"] >= cut and
               (r["type"] in ("handback", "reflect") or (r["type"] == "wake" and r["reason"].startswith(("event:", "heartbeat")))))


def lane_idle(rows: list[dict], st, lane: str, now: datetime) -> float | None:
    """Seconds `lane` has had no run: since its last result or the newest loop start, whichever is later. None while
    a run holds it (or nothing is known). Idle compute is a bug: this is the clock that shows it."""
    if any(st.jobs[j]["claim"]["lane"] == lane for j in st.running()):
        return None
    since = max((r["ts"] for r in rows if (r["t"] == "result" and r.get("cost", {}).get("lane") == lane)
                 or (r["t"] == "auto" and r["type"] == "start")), default=None)
    return max(0.0, (now - B.parse_t(since)).total_seconds()) if since else None


def duration(text: str) -> float:
    """`1h`, `45m`, `2h30m`, `90s` or bare seconds -> seconds."""
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s?)?", text.strip())
    if not text.strip() or not m:
        raise SystemExit(f"--for {text!r}: expected 1h, 45m, 2h30m or seconds")
    h, mi, sec = (float(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + sec


def awake(rows: list[dict]) -> list[str]:
    """Agents with a live sub, read off the tape: their newest spawn (`auto wake` with `upto`) is after their newest sleep row."""
    book, last, out = B.Book(rows), {}, []
    for i, r in enumerate(rows):
        if r["t"] == "auto" and r["type"] == "wake" and "upto" in r:
            last[r["agent"]] = i
        elif r["t"] == "sleep" and book.wallet(r["agent"]) in last:
            last[book.wallet(r["agent"])] = -1
    return sorted(a for a, i in last.items() if i >= 0)


def last_acted(rows: list[dict], book: B.Book, agent: str) -> str | None:
    """ts of the newest post, bet or finding by the agent or its subs."""
    fam = book.family(agent)
    return max((r["ts"] for r in rows if (r["t"] == "bet" and r.get("agent") in fam) or (r["t"] == "node" and (
        r["spec"].get("proposer") if r.get("kind") == "job" else r.get("agent")) in fam)), default=None)


def status(rows: list[dict], cfg: dict, now: datetime) -> dict:
    """For /market.json. last_tick = the newest auto row (ticks that decide nothing write nothing)."""
    auto = [r for r in rows if r["t"] == "auto"]
    life = [r for r in auto if r["type"] in ("start", "stop")]
    return {"running": bool(life) and life[-1]["type"] == "start", "last_tick": auto[-1]["ts"] if auto else None,
            "hour_spend": hour_spend(rows, now), "cap": conf(cfg)["max_usd_per_hour"],
            "subagent_runs_hour": subagent_runs(rows, now), "subagent_cap": conf(cfg)["max_subagent_runs_per_hour"],
            "stopped": bool(life) and life[-1]["type"] == "stop" and life[-1].get("reason") == "STOP file",
            "until": life[-1].get("until") if life and life[-1]["type"] == "start" else None,
            "awake": awake(rows)}


def skill(name: str) -> str:
    text = (REPO / "skills" / name / "SKILL.md").read_text()
    text = text.split("---", 2)[2].strip() if text.startswith("---") else text
    text = text.split("\n## As the dispatcher")[0]     # a sub is an agent, not the dispatcher
    text = re.sub(r" \(if `\$\{CLAUDE_PLUGIN_ROOT\}` is unset[^)]*\)", "", text)
    return text.replace('"${CLAUDE_PLUGIN_ROOT}/bin/q"', "q").replace("${CLAUDE_PLUGIN_ROOT}", str(REPO))


def bootstrap(root) -> str:
    """agents/BOOTSTRAP.md: how to be a member of this market (reflection evolves it; `q bootstrap`)."""
    p = Path(root) / "agents" / "BOOTSTRAP.md"
    return p.read_text().strip() if p.exists() else ""


def stamp(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%S")


class Autopilot:
    def __init__(self, root, lg: L.Ledger, cfg: dict, dry: bool = False, cap: float | None = None, echo=print,
                 sub_cap: int | None = None, for_: str | None = None):
        self.root, self.cfg, self.dry, self.echo = Path(root), cfg, dry, echo
        # a dry run is the same code on an in-memory copy of the ledger, with every spawn replaced by a print
        self.lg = L.MemLedger(lg.host) if dry else lg
        if dry:
            self.lg._rows = list(lg.rows())
        self.c = conf(cfg)
        if cap is not None:
            self.c["max_usd_per_hour"] = cap
        self.for_, self.for_s = for_, duration(for_) if for_ else None
        if sub_cap is not None:
            self.c["max_subagent_runs_per_hour"] = sub_cap
        self.dir = self.root / "autopilot"
        self.notify = REPO / "scripts" / "notify.sh"
        self.runs: dict[str, tuple] = {}     # job -> (lane, Popen)
        self.subs: dict[str, tuple] = {}     # wallet agent -> (sub id, Popen, proposed-before or None)
        self.seen: dict[str, int] = {}       # wallet agent -> ledger row index it has processed up to
        self.base: int | None = None         # first tick: agents with no wake on the tape start here, not at row 0

    # ---- the tape -----------------------------------------------------------------------------------
    def auto(self, type: str, reason: str, lane=None, job=None, agent=None, **kw) -> None:
        row = {"t": "auto", "type": type, "lane": lane, "job": job, "agent": agent, "reason": reason, **kw}
        if type == "refuse":        # a refusal already on the tape for this (lane, job, agent) is not repeated every tick
            last = next((r for r in reversed(self.lg.rows()) if r["t"] == "auto" and r["type"] not in ("start", "stop")
                         and (r.get("lane"), r.get("job"), r.get("agent")) == (lane, job, agent)), None)
            if last and last["type"] == "refuse" and last["reason"] == reason:
                return
        self.lg.append(row)

    # ---- one tick -----------------------------------------------------------------------------------
    def tick(self, now: datetime | None = None) -> bool:
        """False when the STOP file is present (the loop ends)."""
        now = now or datetime.now(timezone.utc)
        self.reap()
        if (self.dir / "STOP").exists():
            self.echo(f"STOP file {self.dir / 'STOP'}: nothing dispatched; the loop ends")
            self.auto("stop", "STOP file")
            return False
        rows = self.lg.rows()
        first = next((r["ts"] for r in rows if r["t"] == "auto" and r["type"] == "start"), None)
        drip = B.tick(self.lg, self.cfg, now, since=first)      # a fresh ledger pays income from the loop's first start, not from "now" forever
        for w in B.wake(self.lg, now):
            self.echo(f"wake: {w['agent']} ({w['reason']})")
            self.auto("wake", wake_tag(w["reason"]), agent=w["agent"])
        if drip:
            self.echo(f"drip: {drip['minutes']} min, ${drip['usd']:.2f} to {', '.join(drip['to']) or 'nobody'}")
        rows = self.lg.rows()
        cap = self.c["max_usd_per_hour"]
        self.echo(f"guardrails: hour spend ${hour_spend(rows, now):.2f}{f' / cap ${cap:.2f}' if cap else ' (no cap)'} · "
                  f"subagents {len(self.subs)}/{self.max_subs()} · "
                  f"subagent_runs_hour {subagent_runs(rows, now)} / {self.c['max_subagent_runs_per_hour']} · STOP file absent")
        self.over_budget(now)
        self.orphans(rows, now)
        self.dispatch(self.lg.rows(), now)
        self.idle_lanes(now)
        bag.regressions(self.lg, self.echo)
        self.desk(now)
        self.settled_by_analysis()
        self.rewake(now)
        self.handback(now)
        self.events(now)
        self.reflection(now)
        if not self.dry:
            L.commit(self.root, self.lg, "autopilot tick")
        return True

    def orphans(self, rows, now):
        """A claim with no result, no release and no live child, older than its kill line (what its funding buys) + 60s, is dead: settle it `invalid` so the lane frees."""
        if self.dry:
            return
        st = L.fold(rows)
        for j in st.running():
            c, s = st.jobs[j]["claim"], st.jobs[j]["spec"]
            if j in self.runs or (now - B.parse_t(c["ts"])).total_seconds() <= specmod.funded_seconds(s, self.cfg["lanes"]) + 60:
                continue
            self.echo(f"auto note {j} orphan")
            lane = c["lane"]
            self.lg.append({"t": "result", "job": j, "verdict": "invalid", "cost": lanes.cost_line(self.cfg, lane),
                            "result": {}, "note": "orphaned claim (no live run)"})
            L.settle(self.lg, s, "invalid")
            B.settle_due(self.lg, self.cfg)
            self.auto("note", "orphan", lane, j)

    def dispatch(self, rows, now):
        st, book = L.fold(rows), B.Book(rows)
        runnable = [j for j in st.frontier() if self.driven(st.jobs[j]["spec"])]
        order, fb = B.order(st, rows, self.cfg, runnable)
        spent, cap = hour_spend(rows, now), self.c["max_usd_per_hour"]
        for lane in [n for n in self.cfg["lanes"] if n != "any"] + ["any"]:
            slots = self.c["any_workers"] if lane == "any" else self.cfg["lanes"][lane].get("slots", 1)
            busy = {j for j in st.running() if st.jobs[j]["claim"]["lane"] == lane} | \
                   {j for j, (l, _) in self.runs.items() if l == lane}
            picks = [j for j in order if st.jobs[j]["spec"]["lane"] == lane and j not in self.runs]
            head = f"lane {lane} ({slots} slot{'s' * (slots > 1)}):"
            if len(busy) >= slots:
                self.echo(f"{head} busy ({', '.join(sorted(busy))})")
                continue
            if not picks:
                self.fill_from_bag(lane, head, st, rows, now, spent, cap)
                continue
            if cap and spent > cap:
                self.echo(f"{head} REFUSED {picks[0]}: hour spend ${spent:.2f} > cap ${cap:.2f}")
                self.auto("refuse", f"hour spend ${spent:.2f} > cap ${cap:.2f}", lane, picks[0])
                continue
            ok, gate = lanes.gate_open(self.cfg, lane)          # 10 s timeout: lanes.gate_open
            if not ok:
                self.echo(f"{head} gate {gate}: REFUSED {picks[0]}")
                self.auto("refuse", f"gate: {gate}", lane, picks[0])
                continue
            for j in picks[:slots - len(busy)]:
                pf = specmod.synth(st.jobs[j]["spec"], self.cfg)
                if pf and pf[1] and (why := bag.preflight(self.root, {"preflight": pf[1]})):
                    reason = f"preflight of {j}: {why}"        # a scenario job's preflight, like a bag draw's
                    if not any(r["t"] == "auto" and r["type"] == "refuse" and r.get("job") == j and r["reason"] == reason
                               and r["ts"] >= B.iso(now - timedelta(minutes=60)) for r in rows):
                        self.auto("refuse", reason, lane, j)
                    self.echo(f"{head} REFUSED {j}: {reason}")
                    break
                s, m = st.jobs[j]["spec"], book.matched(j, st.jobs[j]["spec"])
                why = (f"fallback: nothing matched on {lane}, cheapest at ${s['budget_usd']}" if j in fb or m <= 0 else
                       f"matched ${m:.2f} / budget ${s['budget_usd']} = {m / max(s['budget_usd'], 0.01):.3f} per $")
                self.echo(f"{head} gate {gate} · pick {j} ({why}) · claim + run, funded ${s['budget_usd']} ({specmod.funded_seconds(s, self.cfg['lanes'])}s)")
                self.auto("dispatch", why, lane, j, book.proposers.get(j))
                self.runs[j] = (lane, self.spawn_run(j, now))

    def idle_lanes(self, now, over: float = 0) -> dict[str, float]:
        """lane -> idle seconds for lanes with no run (ledger or this loop's children) idle more than `over`. The first
        time a stretch crosses 5 min it writes one `auto idle` row with `seconds` (the tape and reflection see it)."""
        rows = self.lg.rows()
        st, out = L.fold(rows), {}
        for lane in (n for n in self.cfg["lanes"] if n != "any"):
            s = None if any(l == lane for l, _ in self.runs.values()) else lane_idle(rows, st, lane, now)
            if s is None:
                continue
            if s >= 300 and not any(r["t"] == "auto" and r["type"] == "idle" and r.get("lane") == lane
                                    and r["ts"] >= B.iso(now - timedelta(seconds=s)) for r in rows):
                self.echo(f"auto idle {lane}: {s / 60:.0f} min with no run")
                self.auto("idle", f"lane {lane} idle {s / 60:.0f} min", lane, seconds=round(s))
            if s > over:
                out[lane] = s
        return out

    def driven(self, s: dict) -> bool:
        """The harness can run it: the spec has a `run`, or a `scenario` its lane's runner turns into one."""
        return bool(s.get("run") or specmod.synth(s, self.cfg))

    def fill_from_bag(self, lane, head, st, rows, now, spent, cap):
        """Idle lane (B.order had nothing dispatchable): post one bag draw as `house` and run it. Spend counts against the hour cap, if one is set."""
        c = bag.conf(self.cfg, lane)
        if not c or not c["enabled"]:
            return self.echo(f"{head} free, nothing runnable with a `run`")
        n, no = bag.today(rows, lane, now), f"{head} free, nothing runnable; bag "
        if bag.active(st, lane):
            return self.echo(f"{no}{n}/{c['max_per_day']} today: one bag job at a time")
        if n >= c["max_per_day"]:
            return self.echo(f"{no}{n}/{c['max_per_day']} today{bag.capped(n, c, now)}")
        if cap and spent > cap:
            self.auto("refuse", f"bag: hour spend ${spent:.2f} > cap ${cap:.2f}", lane)
            return self.echo(f"{no}REFUSED: hour spend ${spent:.2f} > cap ${cap:.2f}")
        ok, gate = lanes.gate_open(self.cfg, lane)
        if not ok:
            return self.echo(f"{no}{n}/{c['max_per_day']} today: gate {gate}")
        bo = bag.backoff_until(rows, lane, c)
        if bo and now < bo[0]:
            return self.echo(f"{no}backoff: {bo[1]} consecutive invalid, until {B.iso(bo[0])}")
        drawn = bag.draw(self.root, rows, self.cfg, lane, c, now, self.echo)
        if not drawn:
            return self.echo(f"{no}{n}/{c['max_per_day']} today: empty (no valid spec in {c['specs']})")
        s, last = drawn
        pf = lambda mins: [r for r in rows if r["t"] == "auto" and r["type"] == "refuse" and r.get("lane") == lane
                           and r["reason"].startswith(f"bag {lane} preflight") and r["ts"] >= B.iso(now - timedelta(minutes=mins))]
        if pf(c["backoff_minutes"]):          # a failed preflight backs the lane off like an invalid result
            return self.echo(f"{no}backoff: preflight refused within {c['backoff_minutes']} min")
        if why := bag.preflight(self.root, s):
            reason = f"bag {lane} preflight: {why}"
            if not pf(60):
                self.auto("refuse", reason, lane)
            return self.echo(f"{no}REFUSED: preflight of {s['id']}: {why}")
        spec = bag.post(self.lg, self.cfg, s, lane, c, stamp(now))
        stake = B.Book(self.lg.rows()).totals(spec["id"], "main")["pass"]
        why = f"bag draw {s['id']}, last run {last or 'never'}, house PASS ${stake:.2f}"
        self.echo(f"{head} gate {gate} · BAG {spec['id']} ({why}; "
                  f"bag {n + 1}/{c['max_per_day']} today) · post as house + claim + run, funded ${spec['budget_usd']} ({specmod.funded_seconds(spec, self.cfg['lanes'])}s)")
        self.auto("dispatch", why, lane, spec["id"], B.HOUSE)
        self.runs[spec["id"]] = (lane, self.spawn_run(spec["id"], now))

    def desk(self, now=None):
        """A frontier job with no `run` is work for its proposer: hand it over as a wake, and again on every heartbeat
        window until it has a result (or is cancelled). Oldest job first, one re-hand per proposer per tick, under the
        subagent caps. The 3rd re-hand without a result is flagged `desk-stalled`."""
        now = now or datetime.now(timezone.utc)
        rows = self.lg.rows()
        st, book = L.fold(rows), B.Book(rows)
        front = set(st.frontier())
        cut = B.iso(now - timedelta(minutes=self.c["heartbeat_minutes"]))
        done = {x for r in rows if r["t"] == "auto" and r["type"] == "handback" for x in r.get("refs", [])}
        stalled = {r["job"] for r in rows if r["t"] == "auto" and r["type"] == "note" and r["reason"] == "desk-stalled"}
        handed: set[str] = set()
        for j in (j for j in st.jobs if j in front):
            if self.driven(st.jobs[j]["spec"]):
                continue
            who = book.proposers.get(j)
            if not who or who == B.HUMAN or who not in book.agents:
                self.echo(f"desk {j}: no agent proposer ({who or 'none'}): the session's")
                continue
            w, lane = book.wallet(who), st.jobs[j]["spec"]["lane"]
            wakes = [r for r in rows if r["t"] == "wake" and r["reason"] == f"desk:{j}"]
            if not wakes:
                self.echo(f"desk {j}: wake {w} (desk:{j})")
                self.lg.append({"t": "wake", "agent": w, "reason": f"desk:{j}"})
                self.auto("wake", f"desk:{j}", lane, j, w)
                handed.add(w)
                continue
            last = wakes[-1]
            if (w in handed or w in self.subs or len(self.subs) >= self.max_subs()
                    or f"wake:{last['agent']}:{last['ts']}" not in done or last["ts"] >= cut):
                continue
            self.echo(f"desk {j}: unresolved, re-hand to {w} (desk-retry:{len(wakes)})")
            self.lg.append({"t": "wake", "agent": w, "reason": f"desk:{j}"})
            handed.add(w)
            if len(wakes) == 3 and j not in stalled:      # this is the 3rd re-hand
                self.echo(f"auto note {j} desk-stalled")
                self.auto("note", "desk-stalled", lane, j, w)

    def settled_by_analysis(self):
        """A desk job whose proposer records its own result with no `run` and no claim: flag it once on the tape."""
        rows = self.lg.rows()
        st, book = L.fold(rows), B.Book(rows)
        noted = {r["job"] for r in rows if r["t"] == "auto" and r["type"] == "note" and r["reason"] == "settled-by-analysis"}
        for r in rows:
            j = st.jobs.get(r["job"]) if r["t"] == "result" else None
            who = book.proposers.get(r["job"]) if j else None
            if (who and r.get("agent") == book.wallet(who) and not self.driven(j["spec"]) and r["job"] not in noted
                    and not any(c["t"] == "claim" and c["job"] == r["job"] for c in rows)):
                self.echo(f"auto note {r['job']} settled-by-analysis")
                self.auto("note", "settled-by-analysis", j["spec"]["lane"], r["job"], r["agent"])
                noted.add(r["job"])

    def handback(self, now):
        rows = self.lg.rows()
        st, book = L.fold(rows), B.Book(rows)
        starts = [r["ts"] for r in rows if r["t"] == "auto" and r["type"] == "start"]
        epoch = starts[-1] if starts else B.iso(now)
        dispatched = {r["job"] for r in rows if r["t"] == "auto" and r["type"] == "dispatch"}
        done = {x for r in rows if r["t"] == "auto" and r["type"] == "handback" for x in r.get("refs", [])}
        due: dict[str, list] = {}
        retry: dict[str, str] = {}
        for jid, j in st.jobs.items():
            res, who = j["result"], book.proposers.get(jid)
            if not res or who not in book.agents or j["spec"].get("bag"):     # a bag result goes to nobody: a FAIL's finding is the event
                continue
            ref = f"result:{jid}:{res['ts']}"
            if ref not in done and (jid in dispatched or res["ts"] >= epoch):
                due.setdefault(book.wallet(who), []).append((ref, jid, finished(jid, j)))
        for r in rows:
            if r["t"] == "wake" and r["ts"] >= epoch and r["agent"] in book.agents and f"wake:{r['agent']}:{r['ts']}" not in done:
                jid = r["reason"][5:] if r["reason"].startswith("desk:") else None
                text = (f"Your desk job {jid} is due. It has no run command: do it by hand, then "
                        f"`q result {jid} --verdict pass|fail --as <you>`. The question: {st.jobs[jid]['spec']['question']}\n"
                        + DESK if jid in st.jobs else f"You were woken: {r['reason']}." + (
                            " " + REWAKE.format(gap=f'{self.c["idle_wake_gap_s"]:.0f}') if r["reason"].startswith("rewake") else ""))
                if jid in st.jobs:      # the k-th hand-back of this job: k-1 is the retry number
                    k = sum(1 for x in rows if x["t"] == "wake" and x["reason"] == r["reason"] and x["ts"] <= r["ts"])
                    if k > 1:
                        retry[f"wake:{r['agent']}:{r['ts']}"] = f"desk-retry:{k - 1}"
                due.setdefault(book.wallet(r["agent"]), []).append((f"wake:{r['agent']}:{r['ts']}", jid, text))
        if not due:
            self.echo("hand-backs: none due")
        for agent, items in due.items():
            what = "; ".join(ref for ref, _, _ in items)
            if agent in book.retired:
                continue                   # retired: its results settle, nobody is woken for them
            if agent in self.subs:
                self.echo(f"hand-back {agent}: waits, {self.subs[agent][0]} is still running ({what})")
            elif len(self.subs) >= self.max_subs():
                self.echo(f"hand-back {agent}: REFUSED, subagents at cap {self.max_subs()} ({what})")
                self.auto("refuse", f"subagents at cap {self.max_subs()}", agent=agent)
            else:
                sub = self.register(agent, f"autopilot hand-back: {what}", now)
                self.echo(f"hand-back {agent}: spawn {sub} ({self.label(book.agents[agent])}) for {what}")
                self.auto("handback", "; ".join(retry.get(ref, ref) for ref, _, _ in items), job=next((j for _, j, _ in items if j), None), agent=sub,
                          refs=[ref for ref, _, _ in items])
                self.echo_wake(agent, f"handback:{next((j for _, j, _ in items if j), items[0][0])}")   # no job: name the wake/result ref
                self.subs[agent] = (sub, self.spawn(sub, book.agents[agent], self.prompt(sub, agent, items)), None)
                self.seen[agent] = len(self.lg.rows())

    def echo_wake(self, agent: str, reason: str, **kw):
        self.auto("wake", reason, agent=agent, upto=len(self.lg.rows()), **kw)

    def events(self, now):
        """Event-driven wakes: every persistent agent without an explicit sleep and without a live sub is asleep
        until-event. If board events (B.board_events) landed since it last looked, wake it: one sub per agent per
        tick, oldest event first; every sub is a Popen so they all run at once. A refused wake is retried next tick
        (`seen` does not move); events that land while its sub runs batch into its next wake."""
        rows = self.lg.rows()
        book = B.Book(rows)
        if self.base is None:
            self.base = len(rows)
            self.t0 = B.iso(now)      # the heartbeat clock starts with the loop
        due = []
        for a, r in book.agents.items():
            if a not in book.active() or a == B.REFLECT or a in self.subs or B.explicit_sleep(rows, book, a):
                continue
            if a not in self.seen:
                self.seen[a] = next((x["upto"] for x in reversed(rows) if x["t"] == "auto" and x["type"] == "wake"
                                     and x["agent"] == a and "upto" in x), self.base)
            ev = B.board_events(rows, book, book.family(a), self.seen[a])
            if ev:
                due.append((ev[0], a, ev))
            elif self.idle(rows, book, a, now):
                due.append((len(rows), a, []))
        for _, a, ev in sorted(due):
            why = f"event:{len(ev)} rows" if ev else "heartbeat"
            if a in self.subs:          # re-woken this tick (rewake -> handback)
                continue
            if len(self.subs) >= self.max_subs():
                self.echo(f"wake {a}: REFUSED, subagents at cap {self.max_subs()} ({why})")
                self.auto("refuse", f"subagents at cap {self.max_subs()}", agent=a)
            else:
                sub = self.register(a, f"autopilot wake: {why}", now)
                self.echo(f"wake {a}: spawn {sub} ({self.label(book.agents[a])}) for {why}")
                self.echo_wake(a, why)
                text = "Since you last looked:\n" + B.digest(self.lg.rows(), ev) if ev else \
                    f"Nothing on the board has moved for you in {self.c['heartbeat_minutes']} minutes."
                self.subs[a] = (sub, self.spawn(sub, book.agents[a], self.prompt(sub, a, [], text)), None)
                self.seen[a] = len(self.lg.rows())

    def idle(self, rows, book, a, now) -> bool:
        """Heartbeat: funded, and no post/bet/finding, wake or loop start in the last heartbeat window."""
        cut = B.iso(now - timedelta(minutes=self.c["heartbeat_minutes"]))
        woke = max((r["ts"] for r in rows if r["t"] == "auto" and r["type"] == "wake" and r["agent"] == a), default="")
        return book.balance(a) >= self.c["heartbeat_min_usd"] and max(last_acted(rows, book, a) or "", woke, self.t0) < cut

    def max_subs(self) -> int:
        """[autopilot] max_concurrent_subagents if set, else one per persistent agent + 1 for the reflection pass."""
        return self.c.get("max_concurrent_subagents") or 1 + sum(1 for a in B.Book(self.lg.rows()).active() if a != B.REFLECT)

    def over_budget(self, now):
        """max_subagent_runs_per_hour is a budget, not a gate: over it, one `auto refuse` row per hour, nothing blocked."""
        rows, cap = self.lg.rows(), self.c["max_subagent_runs_per_hour"]
        n, cut = subagent_runs(rows, now), B.iso(now - timedelta(hours=1))
        if n >= cap and not any(r["t"] == "auto" and r["type"] == "refuse" and r["reason"].startswith("subagent budget")
                                and r["ts"] >= cut for r in rows):
            self.echo(f"subagent budget: {n} turns in the last hour >= {cap} (soft: nothing is blocked)")
            self.auto("refuse", f"subagent budget {n}/{cap} per hour (soft, not blocking)")

    def rewake(self, now):
        """Agents never sleep: every persistent agent with no live sub is re-woken idle_wake_gap_s
        after its last turn ended (its newest sleep row: every turn leaves one, ensure_sleep), in flight or not. Sleep
        rows are notes, not gates; a result of its own still hands back at once (handback). Appends a `wake` row that
        handback spawns this same tick."""
        rows = self.lg.rows()
        st, book, gap = L.fold(rows), B.Book(rows), self.c["idle_wake_gap_s"]
        starts = [r["ts"] for r in rows if r["t"] == "auto" and r["type"] == "start"]
        epoch = starts[-1] if starts else B.iso(now)
        done = {x for r in rows if r["t"] == "auto" and r["type"] == "handback" for x in r.get("refs", [])}
        live = set(awake(rows))
        for a in book.active():
            if a == B.REFLECT:
                continue
            fam = book.family(a)
            fl = sorted(j for j, x in st.jobs.items() if book.proposers.get(j) in fam
                        and x["state"] in ("queued", "running") and self.driven(x["spec"]))
            head = f"agent {a}: " + (f"in flight {', '.join(fl)}" if fl else "idle")
            if a in self.subs:
                self.echo(f"{head} · sub {self.subs[a][0]} alive")
                continue
            if any(r["t"] == "wake" and r["agent"] == a and r["ts"] >= epoch and f"wake:{a}:{r['ts']}" not in done for r in rows):
                self.echo(f"{head} · wake pending")
                continue
            spawn = max((r["ts"] for r in rows if r["t"] == "auto" and r["type"] == "wake" and r["agent"] == a and "upto" in r), default="")
            # ponytail: a sub of the previous loop process is assumed alive for 30 min after its spawn, then presumed dead
            if a in live and spawn >= B.iso(now - timedelta(minutes=30)):
                self.echo(f"{head} · sub of a previous loop alive (spawned {spawn[11:19]}Z)")
                continue
            end = max((r["ts"] for r in rows if r["t"] == "sleep" and r["agent"] in fam), default=None)
            left = gap - (now - B.parse_t(end)).total_seconds() if end else 0
            if left > 0:
                self.echo(f"{head} · re-wake in {left:.0f}s")
                continue
            why = f"rewake: {'in flight ' + ', '.join(fl) if fl else 'nothing in flight'}"
            self.echo(f"{head} -> wake ({why})")
            self.lg.append({"t": "wake", "agent": a, "reason": why}, B.iso(now))

    def reflection(self, now):
        rows = self.lg.rows()
        why = reflect.due(rows, now, self.cfg)
        if not why:
            self.echo("reflection: not due")
            return
        if B.REFLECT in self.subs or any(r["t"] == "auto" and r["type"] == "reflect" for r in reflect.since_last(rows)):
            self.echo(f"reflection: due ({why}); this cycle's pass already ran: the session reviews queue/proposed/ "
                      f"and runs `q reflect --record --as reflect`")
            return
        if len(self.subs) >= self.max_subs():
            self.echo(f"reflection: due ({why}); REFUSED, subagents at cap")
            self.auto("refuse", f"subagents at cap {self.max_subs()}", agent=B.REFLECT)
            return
        if B.REFLECT not in B.Book(rows).agents:
            self.echo("reflection: registering `reflect` (persistent)")
            self.lg.append(B.agent_row(B.Book(rows), B.REFLECT, "reflection passes: patterns across jobs and findings"))
            if not self.dry:
                (self.root / "agents").mkdir(exist_ok=True)
                (self.root / "agents" / "reflect.toml").write_text(
                    'id = "reflect"\nkind = "persistent"\nbrief = "reflection passes: patterns across jobs and findings"\n')
        sub = self.register(B.REFLECT, f"autopilot reflection pass ({why})", now)
        book = B.Book(self.lg.rows())
        rc = self.c["reflect"]
        who = {"runtime": rc.get("runtime", "claude"), "model": rc.get("model") or ("opus" if rc.get("runtime", "claude") == "claude" else None)}
        self.echo(f"reflection: due ({why}); spawn {sub} ({self.label(who)}); proposals go to queue/proposed/, never added")
        self.auto("reflect", why, agent=sub)
        text = "\n\n".join([skill("reflect"), "Standing rules (verbatim): " + RULES,
                            f"You are the ONE Opus subagent of step 2, acting as {sub} (a sub of reflect: `--as {sub}`). "
                            f"The CLI is {REPO}/bin/q (PIT_ROOT is set). Do step 2's brief: write proposals to "
                            f"{self.root}/queue/proposed/<id>.toml. Never run q add, q post or q reflect --record: "
                            f"steps 3 and 4 are the session's. Bet as `reflect` against every open run whose proposer you think is "
                            f"overconfident; say why in one line each.",
                            STRUCTURE, "Agents on the book: " + json.dumps({a: {"brief": r["brief"], "retired": a in book.retired}
                                                                            for a, r in book.agents.items() if r["kind"] == "persistent"}),
                            BOOTSTRAP_LOOP, "agents/BOOTSTRAP.md now:\n" + bootstrap(self.root),
                            "Newcomer (most recently registered agent) bootstrap cost: " + json.dumps(B.newcomer_cost(rows)),
                            "Digest (q reflect --since-last):\n" + reflect.digest(rows)])
        self.subs[B.REFLECT] = (sub, self.spawn(sub, who, text), self.proposed())

    # ---- children -----------------------------------------------------------------------------------
    def register(self, agent: str, brief: str, now: datetime) -> str:
        book, sub, n = B.Book(self.lg.rows()), f"{agent}-{stamp(now)}", 1
        while sub in book.agents:
            n += 1
            sub = f"{agent}-{stamp(now)}-{n}"
        self.lg.append(B.agent_row(book, sub, brief, agent))
        return sub

    def prompt(self, sub: str, agent: str, items: list, digest: str = "") -> str:
        rows = self.lg.rows()
        if not digest:      # a hand-back also gets the digest of what else moved since its last look
            ev = B.board_events(rows, B.Book(rows), B.Book(rows).family(agent), self.seen.get(agent, len(rows)))
            digest = "Since you last looked:\n" + B.digest(rows, ev) if ev else ""
        idle = "\n".join(f"Lane {l} has been idle {s / 60:.0f} min. Idle compute is a bug. Post a runnable experiment on it "
                         f"this turn (`q list --scenarios`), or say in one line why nothing worth running exists."
                         for l, s in self.idle_lanes(datetime.now(timezone.utc), 120).items())
        return "\n\n".join(filter(None, [
            idle, skill("pit"), "Standing rules (verbatim): " + RULES, bootstrap(self.root),
            f"You are {sub}, a sub of {agent}: act `--as {sub}`; your bets and posts book to {agent}. "
            f"The CLI is {REPO}/bin/q (on PATH as q; PIT_ROOT is set).",
            f"q thread {agent}:\n" + B.thread(self.lg.rows(), agent),
            "\n".join(t for _, _, t in items),
            f"Your claim ({agent}'s brief): {B.Book(rows).agents[agent]['brief']}", CLAIM, digest, BOARD, "q board:\n" + B.board(rows, self.cfg),
            f"You MUST end your turn by saying what you are waiting on (`q sleep --as {sub} --until-result <job>`, or "
            f"`q sleep --as {sub} --until-event --note '<what>'`), then stop; you will be woken again in about "
            f"{self.c['idle_wake_gap_s']:.0f} s (a result of yours wakes you at once). Every turn must leave the market changed: "
            f"a post, a bet, or a finding."]))

    def env(self) -> dict:
        return {**os.environ, "PIT_ROOT": str(self.root), "PATH": f"{REPO / 'bin'}:{os.environ.get('PATH', '')}",
                "PYTHONPATH": f"{REPO}:{os.environ.get('PYTHONPATH', '')}".rstrip(":")}

    def log(self, name: str):
        (self.dir / "logs").mkdir(parents=True, exist_ok=True)
        return self.dir / "logs" / name

    def spawn_run(self, jid: str, now: datetime):
        if self.dry:
            return None
        with self.log(f"run-{jid}-{stamp(now)}.log").open("w") as out:   # the child keeps its own handle
            # own session: a Ctrl-C of the loop never kills a run between its claim and its result
            return subprocess.Popen([sys.executable, "-m", "pit.cli", "--root", str(self.root), "run", jid],
                                    stdout=out, stderr=subprocess.STDOUT, cwd=self.root, env=self.env(),
                                    start_new_session=True)

    def permission_mode(self) -> str:
        """[autopilot] permission_mode, else the repo's (then the user's) Claude Code defaultMode."""
        if self.c["permission_mode"]:
            return self.c["permission_mode"]
        for f in (self.root / ".claude" / "settings.json", REPO / ".claude" / "settings.json",
                  Path.home() / ".claude" / "settings.json"):
            try:
                mode = json.loads(f.read_text()).get("permissions", {}).get("defaultMode")
            except (OSError, ValueError):
                continue
            if mode:
                return mode
        return "auto"

    def model(self, row: dict) -> tuple[str, str | None]:
        """(runtime, model) of an agent row: its own, else [autopilot] runtimes.<runtime>.model, else sonnet for
        claude and the codex CLI's own configured default for codex (None: no -m)."""
        rt = row.get("runtime") or "claude"
        return rt, row.get("model") or self.c["runtimes"].get(rt, {}).get("model") or ("sonnet" if rt == "claude" else None)

    def label(self, row: dict) -> str:
        rt, model = self.model(row)
        return f"{rt}/{model or 'default'}"

    def argv(self, exe: str, rt: str, model: str | None) -> list[str]:
        """The command a turn runs; the prompt goes on stdin for both runtimes. Extra args: runtimes.<rt>.args."""
        dirs = [x for d in self.c.get("add_dirs", []) for x in ("--add-dir", os.path.expanduser(d))]   # repos a desk job may touch
        if rt == "codex":      # its workspace-write sandbox keeps .git read-only: q commits the ledger, so .git dirs are added
            gits = [x for d in (self.root, *map(os.path.expanduser, self.c.get("add_dirs", [])))
                    if (Path(d) / ".git").is_dir() for x in ("--add-dir", str(Path(d) / ".git"))]
            args = self.c["runtimes"].get("codex", {}).get("args", CODEX_ARGS)
            return [exe, "exec", *(["-m", model] if model else []), *args, *dirs, *gits, "-C", str(self.root), "-"]
        cmd = [exe, "-p", "--model", model, "--permission-mode", self.permission_mode(), "--output-format", "text"]
        if self.c.get("allowed_tools"):
            cmd += ["--allowedTools", ",".join(self.c["allowed_tools"])]
        return cmd + dirs + self.c["runtimes"].get("claude", {}).get("args", [])

    def spawn(self, sub: str, row: dict, prompt: str):
        """Spawn one turn of `sub` on its agent row's runtime; output to autopilot/logs/<sub>.log."""
        rt, model = self.model(row)
        if rt not in B.RUNTIMES:
            raise SystemExit(f"unknown runtime {rt}")
        exe = shutil.which(rt)
        if self.dry or not exe:
            self.echo(f"--- prompt for {sub} ({rt}/{model or 'default'}){'' if exe else f': {rt} is not on PATH'} ---\n"
                      f"{prompt}\n--- end of prompt for {sub} ---")
            return None
        p = self.log(f"{sub}.prompt")
        p.write_text(prompt)
        with p.open() as stdin, self.log(f"{sub}.log").open("w") as out:
            return subprocess.Popen(self.argv(exe, rt, model), stdin=stdin, stdout=out, stderr=subprocess.STDOUT,
                                    cwd=self.root, env=self.env(), start_new_session=True)

    def proposed(self) -> set[str]:
        return {p.name for p in (self.root / "queue" / "proposed").glob("*.toml")}

    def reap(self):
        for j, (lane, p) in list(self.runs.items()):
            if p is None or p.poll() is not None:
                self.echo(f"run {j} on {lane} exited {p.returncode if p else 'dry'}")
                del self.runs[j]
        for agent, (sub, p, before) in list(self.subs.items()):
            if p is not None and p.poll() is None:
                continue
            del self.subs[agent]
            self.echo(f"{sub} finished (exit {p.returncode if p else '-'}): autopilot/logs/{sub}.log")
            if before is None and p is not None:
                self.ensure_sleep(agent, sub)
            if before is not None:
                new = sorted(self.proposed() - before)
                for name in new:
                    self.echo(f"proposed by {sub}: queue/proposed/{name}\n" + (self.root / "queue" / "proposed" / name).read_text())
                if p is not None:
                    subprocess.run([str(self.notify), f"Pit reflection {sub}: {len(new)} proposed "
                                    f"({', '.join(new) or 'none'}); review queue/proposed/, then q reflect --record"])

    def ensure_sleep(self, agent: str, sub: str):
        """An agent never just ends: a sub that left no sleep row gets `sleep until-event`."""
        rows = self.lg.rows()
        i = next(i for i, r in enumerate(rows) if r["t"] == "agent" and r["id"] == sub)
        if not any(r["t"] == "sleep" and r["agent"] in (sub, agent) for r in rows[i:]):
            self.echo(f"{sub} ended without sleeping: auto sleep until-event for {agent}")
            self.lg.append({"t": "sleep", "agent": agent, "until": {"event": True}, "note": "auto: sub ended without sleeping"})
            self.auto("sleep", "sub ended without sleeping", agent=agent)

    def wait(self):
        for _, p in self.runs.values():
            p and p.wait()
        for _, p, _ in self.subs.values():
            p and p.wait()
        self.reap()

    def loop(self, interval: float = 60, once: bool = False) -> None:
        now = datetime.now(timezone.utc)
        self.echo(f"autopilot {'plan (dry run: the ledger is not written; nothing is claimed, run or spawned)' if self.dry else 'on'}"
                  f" @ {B.iso(now)} · root {self.root}")
        end = now + timedelta(seconds=self.for_s) if self.for_ else None
        if end:
            self.echo(f"deadline: for {self.for_}, until {B.iso(end)} (the last tick starts before it; runs in flight are never killed)")
        self.auto("start", f"interval {interval}s, " + (f"cap ${self.c['max_usd_per_hour']}/h" if self.c["max_usd_per_hour"] else "no spend cap") + (", once" if once else ""),
                  until=B.iso(end) if end else None)
        try:
            while self.tick():
                if once:
                    if not self.dry:
                        self.wait()
                    self.auto("stop", "once")
                    break
                left = (end - datetime.now(timezone.utc)).total_seconds() if end else interval
                if left <= 0 or (self.dry and end):
                    self.auto("stop", f"for {self.for_} elapsed")
                    self.echo("deadline reached: running children continue: " + (", ".join(self.runs) or "none"))
                    break
                time.sleep(min(interval, left))
                if end and datetime.now(timezone.utc) >= end:      # nothing new is dispatched after the deadline
                    self.auto("stop", f"for {self.for_} elapsed")
                    self.echo("deadline reached: running children continue: " + (", ".join(self.runs) or "none"))
                    break
        except KeyboardInterrupt:
            self.auto("stop", "interrupted")
            self.echo("interrupted; running children continue: " + (", ".join(self.runs) or "none"))
        if not self.dry:
            L.commit(self.root, self.lg, "autopilot stop")


def wake_tag(reason: str) -> str:
    """B.wake's free text -> the short reason on the tape: balance / result:<job> / market:<job> / minutes / desk:<job>."""
    if reason.startswith("balance"):
        return "balance"
    if reason.endswith("min passed"):
        return "minutes"
    if " ended " in reason:
        return "result:" + reason.split(" ")[0]
    if "'s market moved" in reason:
        return "market:" + reason.split("'s")[0]
    return reason


def finished(jid: str, j: dict) -> str:
    res, s = j["result"], j["spec"]
    c = res.get("cost", {})
    branch = s.get(f"if_{res['verdict']}")
    return (f"Your job {jid} finished: {res['verdict']}, {json.dumps(res.get('result', {}))}, "
            f"cost ${c.get('usd', 0):.2f} ({c.get('wall_s', 0):.0f}s on {c.get('lane', '?')}, meters {json.dumps(c.get('meters', {}))})." + (f" Note: {res['note']}." if res.get("note") else "")
            + (f" The spec's {res['verdict']} branch: {branch}" if branch else ""))
