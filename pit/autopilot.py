"""`q autopilot`: the loop that runs Pit between sessions (a state machine over the ledger).

Each tick: STOP file? -> B.tick (drip + wakes) -> per lane: free? pick B.order()[0] among jobs with a `run`,
gate, hour cap -> claim+run in a subprocess (`q run`, so lanes run at once; `any` gets a small worker pool) ->
desk jobs (no `run`) become a `wake` for their proposer -> hand-backs: every result of a posted job and every
wake spawns the proposer as a Claude Code subagent (a sub `<agent>-<ts>`, so it books to the parent) -> reflection
when reflect.due() fires (Opus, a sub of `reflect`; proposals are printed and the session notified, never added).
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

from . import bag, lanes, ledger as L, book as B, reflect

REPO = Path(__file__).resolve().parent.parent
RULES = ("Money is a scheduling signal, not real. Never spend real money, never send messages to people, never touch "
         "production; bench runs use mocks only; nothing over an hour.")
CLAIM = ("Your brief is a claim to prove or refute; it is your goal. This turn: (1) state the claim in one line and the "
         "strongest evidence for and against it from your thread. (2) Your next experiment: name the cheapest run "
         "that could change your mind and post it (`q post --as <you>` — you pay; if you cannot afford the lane you "
         "want, post it on a cheaper lane or `q sleep --until-balance` naming the run). Posting is the primary action "
         "every turn unless you are waiting on a run of yours; or record a finding if your evidence already settles something. If a result of yours settles a question by "
         "analysis and implies a concrete change or run, post that change as your next job before you sleep.")
BOARD = ("The board is context. If an open run bears on your claim you may bet on it (that is how you get paid for "
         "understanding what others are finding), and you may bet where you have a reason even if it does not. The market "
         "is not the goal: it buys you time on the machines and pays you for understanding.")
DEFAULTS = {"max_usd_per_hour": 40.0, "max_concurrent_subagents": 3, "any_workers": 2, "permission_mode": "",
            "max_subagent_runs_per_hour": 12, "heartbeat_minutes": 20, "heartbeat_min_usd": 1.0}


def conf(cfg: dict) -> dict:
    return {**DEFAULTS, **cfg.get("autopilot", {})}


def hour_spend(rows: list[dict], now: datetime) -> float:
    cut = B.iso(now - timedelta(hours=1))
    return round(sum(r["cost"]["usd"] for r in rows if r["t"] == "result" and r["ts"] >= cut), 4)


def subagent_runs(rows: list[dict], now: datetime) -> int:
    """Real-token spawns (hand-backs, event wakes + reflections) in the last hour."""
    cut = B.iso(now - timedelta(hours=1))
    return sum(1 for r in rows if r["t"] == "auto" and r["ts"] >= cut and
               (r["type"] in ("handback", "reflect") or (r["type"] == "wake" and r["reason"].startswith(("event:", "heartbeat")))))


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
        drip = B.tick(self.lg, self.cfg, now)
        for w in B.wake(self.lg, now):
            self.echo(f"wake: {w['agent']} ({w['reason']})")
            self.auto("wake", wake_tag(w["reason"]), agent=w["agent"])
        if drip:
            self.echo(f"drip: {drip['minutes']} min, ${drip['usd']:.2f} to {', '.join(drip['to']) or 'nobody'}")
        rows = self.lg.rows()
        self.echo(f"guardrails: hour spend ${hour_spend(rows, now):.2f} / cap ${self.c['max_usd_per_hour']:.2f} · "
                  f"subagents {len(self.subs)}/{self.c['max_concurrent_subagents']} · "
                  f"subagent_runs_hour {subagent_runs(rows, now)} / {self.c['max_subagent_runs_per_hour']} · STOP file absent")
        self.dispatch(rows, now)
        bag.regressions(self.lg, self.echo)
        self.desk()
        self.settled_by_analysis()
        self.handback(now)
        self.events(now)
        self.reflection(now)
        if not self.dry:
            L.commit(self.root, self.lg, "autopilot tick")
        return True

    def dispatch(self, rows, now):
        st, book = L.fold(rows), B.Book(rows)
        runnable = [j for j in st.frontier() if st.jobs[j]["spec"].get("run")]
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
            if spent > cap:
                self.echo(f"{head} REFUSED {picks[0]}: hour spend ${spent:.2f} > cap ${cap:.2f}")
                self.auto("refuse", f"hour spend ${spent:.2f} > cap ${cap:.2f}", lane, picks[0])
                continue
            ok, gate = lanes.gate_open(self.cfg, lane)          # 10 s timeout: lanes.gate_open
            if not ok:
                self.echo(f"{head} gate {gate}: REFUSED {picks[0]}")
                self.auto("refuse", f"gate: {gate}", lane, picks[0])
                continue
            for j in picks[:slots - len(busy)]:
                s, m = st.jobs[j]["spec"], book.matched(j, st.jobs[j]["spec"])
                why = (f"fallback: nothing matched on {lane}, cheapest at ${s['budget_usd']}" if j in fb or m <= 0 else
                       f"matched ${m:.2f} / budget ${s['budget_usd']} = {m / max(s['budget_usd'], 0.01):.3f} per $")
                self.echo(f"{head} gate {gate} · pick {j} ({why}) · claim + run, stop at {2 * s['budget_s']}s")
                self.auto("dispatch", why, lane, j, book.proposers.get(j))
                self.runs[j] = (lane, self.spawn_run(j, now))

    def fill_from_bag(self, lane, head, st, rows, now, spent, cap):
        """Idle lane (B.order had nothing dispatchable): post one bag draw as `house` and run it. Spend counts against the hour cap."""
        c = bag.conf(self.cfg, lane)
        if not c or not c["enabled"]:
            return self.echo(f"{head} free, nothing runnable with a `run`")
        n, no = bag.today(rows, lane, now), f"{head} free, nothing runnable; bag "
        if bag.active(st, lane):
            return self.echo(f"{no}{n}/{c['max_per_day']} today: one bag job at a time")
        if n >= c["max_per_day"]:
            return self.echo(f"{no}{n}/{c['max_per_day']} today: cap reached")
        if spent > cap:
            self.auto("refuse", f"bag: hour spend ${spent:.2f} > cap ${cap:.2f}", lane)
            return self.echo(f"{no}REFUSED: hour spend ${spent:.2f} > cap ${cap:.2f}")
        ok, gate = lanes.gate_open(self.cfg, lane)
        if not ok:
            return self.echo(f"{no}{n}/{c['max_per_day']} today: gate {gate}")
        drawn = bag.draw(self.root, rows, self.cfg, lane, c, now, self.echo)
        if not drawn:
            return self.echo(f"{no}{n}/{c['max_per_day']} today: empty (no valid spec in {c['specs']})")
        s, last = drawn
        spec = bag.post(self.lg, self.cfg, s, lane, c, stamp(now))
        stake = B.Book(self.lg.rows()).totals(spec["id"], "main")["pass"]
        why = f"bag draw {s['id']}, last run {last or 'never'}, house PASS ${stake:.2f}"
        self.echo(f"{head} gate {gate} · BAG {spec['id']} ({why}; ${spec['budget_usd']} / {spec['budget_s']}s; "
                  f"bag {n + 1}/{c['max_per_day']} today) · post as house + claim + run, stop at {2 * spec['budget_s']}s")
        self.auto("dispatch", why, lane, spec["id"], B.HOUSE)
        self.runs[spec["id"]] = (lane, self.spawn_run(spec["id"], now))

    def desk(self):
        """A frontier job with no `run` is work for its proposer: hand it over as a wake (once)."""
        rows = self.lg.rows()
        st, book = L.fold(rows), B.Book(rows)
        woken = {r["reason"] for r in rows if r["t"] == "wake"}
        for j in st.frontier():
            if st.jobs[j]["spec"].get("run") or f"desk:{j}" in woken:
                continue
            who = book.proposers.get(j)
            if not who or who == B.HUMAN or who not in book.agents:
                self.echo(f"desk {j}: no agent proposer ({who or 'none'}): the session's")
                continue
            self.echo(f"desk {j}: wake {book.wallet(who)} (desk:{j})")
            self.lg.append({"t": "wake", "agent": book.wallet(who), "reason": f"desk:{j}"})
            self.auto("wake", f"desk:{j}", st.jobs[j]["spec"]["lane"], j, book.wallet(who))

    def settled_by_analysis(self):
        """A desk job whose proposer records its own result with no `run` and no claim: flag it once on the tape."""
        rows = self.lg.rows()
        st, book = L.fold(rows), B.Book(rows)
        noted = {r["job"] for r in rows if r["t"] == "auto" and r["type"] == "note" and r["reason"] == "settled-by-analysis"}
        for r in rows:
            j = st.jobs.get(r["job"]) if r["t"] == "result" else None
            who = book.proposers.get(r["job"]) if j else None
            if (who and r.get("agent") == book.wallet(who) and not j["spec"].get("run") and r["job"] not in noted
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
        for jid, j in st.jobs.items():
            res, who = j["result"], book.proposers.get(jid)
            if not res or who not in book.agents:
                continue
            ref = f"result:{jid}:{res['ts']}"
            if ref not in done and (jid in dispatched or res["ts"] >= epoch):
                due.setdefault(book.wallet(who), []).append((ref, jid, finished(jid, j)))
        for r in rows:
            if r["t"] == "wake" and r["ts"] >= epoch and r["agent"] in book.agents and f"wake:{r['agent']}:{r['ts']}" not in done:
                jid = r["reason"][5:] if r["reason"].startswith("desk:") else None
                text = (f"Your desk job {jid} is due. It has no run command: do it by hand, then "
                        f"`q result {jid} --verdict pass|fail --as <you>`. The question: {st.jobs[jid]['spec']['question']}"
                        if jid in st.jobs else f"You were woken: {r['reason']}.")
                due.setdefault(book.wallet(r["agent"]), []).append((f"wake:{r['agent']}:{r['ts']}", jid, text))
        if not due:
            self.echo("hand-backs: none due")
        for agent, items in due.items():
            what = "; ".join(ref for ref, _, _ in items)
            if agent in self.subs:
                self.echo(f"hand-back {agent}: waits, {self.subs[agent][0]} is still running ({what})")
            elif len(self.subs) >= self.c["max_concurrent_subagents"]:
                self.echo(f"hand-back {agent}: REFUSED, subagents at cap {self.c['max_concurrent_subagents']} ({what})")
                self.auto("refuse", f"subagents at cap {self.c['max_concurrent_subagents']}", agent=agent)
            elif self.capped(now):
                self.echo(f"hand-back {agent}: REFUSED, subagent cap ({what}); retried next tick")
                self.auto("refuse", "subagent cap", agent=agent)
            else:
                sub = self.register(agent, f"autopilot hand-back: {what}", now)
                self.echo(f"hand-back {agent}: spawn {sub} (sonnet) for {what}")
                self.auto("handback", what, job=next((j for _, j, _ in items if j), None), agent=sub,
                          refs=[ref for ref, _, _ in items])
                self.echo_wake(agent, f"handback:{next((j for _, j, _ in items if j), '?')}")
                self.subs[agent] = (sub, self.claude(sub, "sonnet", self.prompt(sub, agent, items)), None)
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
            if r["kind"] != "persistent" or a == B.REFLECT or a in self.subs or B.explicit_sleep(rows, book, a):
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
            if len(self.subs) >= self.c["max_concurrent_subagents"]:
                self.echo(f"wake {a}: REFUSED, subagents at cap {self.c['max_concurrent_subagents']} ({why})")
                self.auto("refuse", f"subagents at cap {self.c['max_concurrent_subagents']}", agent=a)
            elif self.capped(now):
                self.echo(f"wake {a}: REFUSED, subagent cap ({why}); retried next tick")
                self.auto("refuse", "subagent cap", agent=a)
            else:
                sub = self.register(a, f"autopilot wake: {why}", now)
                self.echo(f"wake {a}: spawn {sub} (sonnet) for {why}")
                self.echo_wake(a, why)
                text = "Since you last looked:\n" + B.digest(self.lg.rows(), ev) if ev else \
                    f"Nothing on the board has moved for you in {self.c['heartbeat_minutes']} minutes."
                self.subs[a] = (sub, self.claude(sub, "sonnet", self.prompt(sub, a, [], text)), None)
                self.seen[a] = len(self.lg.rows())

    def idle(self, rows, book, a, now) -> bool:
        """Heartbeat: funded, and no post/bet/finding, wake or loop start in the last heartbeat window."""
        cut = B.iso(now - timedelta(minutes=self.c["heartbeat_minutes"]))
        woke = max((r["ts"] for r in rows if r["t"] == "auto" and r["type"] == "wake" and r["agent"] == a), default="")
        return book.balance(a) >= self.c["heartbeat_min_usd"] and max(last_acted(rows, book, a) or "", woke, self.t0) < cut

    def capped(self, now) -> bool:
        return subagent_runs(self.lg.rows(), now) >= self.c["max_subagent_runs_per_hour"]

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
        if len(self.subs) >= self.c["max_concurrent_subagents"]:
            self.echo(f"reflection: due ({why}); REFUSED, subagents at cap")
            self.auto("refuse", f"subagents at cap {self.c['max_concurrent_subagents']}", agent=B.REFLECT)
            return
        if self.capped(now):
            self.echo(f"reflection: due ({why}); REFUSED, subagent cap; retried next tick")
            self.auto("refuse", "subagent cap", agent=B.REFLECT)
            return
        if B.REFLECT not in B.Book(rows).agents:
            self.echo("reflection: registering `reflect` (persistent)")
            self.lg.append(B.agent_row(B.Book(rows), B.REFLECT, "reflection passes: patterns across jobs and findings"))
            if not self.dry:
                (self.root / "agents").mkdir(exist_ok=True)
                (self.root / "agents" / "reflect.toml").write_text(
                    'id = "reflect"\nkind = "persistent"\nbrief = "reflection passes: patterns across jobs and findings"\n')
        sub = self.register(B.REFLECT, f"autopilot reflection pass ({why})", now)
        self.echo(f"reflection: due ({why}); spawn {sub} (opus); proposals go to queue/proposed/, never added")
        self.auto("reflect", why, agent=sub)
        text = "\n\n".join([skill("reflect"), "Standing rules (verbatim): " + RULES,
                            f"You are the ONE Opus subagent of step 2, acting as {sub} (a sub of reflect: `--as {sub}`). "
                            f"The CLI is {REPO}/bin/q (PIT_ROOT is set). Do step 2's brief: write proposals to "
                            f"{self.root}/queue/proposed/<id>.toml. Never run q add, q post or q reflect --record: "
                            f"steps 3 and 4 are the session's.",
                            "Digest (q reflect --since-last):\n" + reflect.digest(rows)])
        self.subs[B.REFLECT] = (sub, self.claude(sub, "opus", text), self.proposed())

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
        return "\n\n".join(filter(None, [
            skill("pit"), "Standing rules (verbatim): " + RULES,
            f"You are {sub}, a sub of {agent}: act `--as {sub}`; your bets and posts book to {agent}. "
            f"The CLI is {REPO}/bin/q (on PATH as q; PIT_ROOT is set).",
            f"q thread {agent}:\n" + B.thread(self.lg.rows(), agent),
            "\n".join(t for _, _, t in items),
            f"Your claim ({agent}'s brief): {B.Book(rows).agents[agent]['brief']}", CLAIM, digest, BOARD,
            f"You MUST end your turn with exactly one sleep: `q sleep --as {sub} --until-result <your job>` when you have "
            f"a run in flight, `--until-event` otherwise (`--until-balance N`, `--minutes N` also work), with a one-line "
            f"note. Passing (posting nothing) is allowed only with a one-line reason. Never just stop."]))

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

    def claude(self, sub: str, model: str, prompt: str):
        exe = shutil.which("claude")
        if self.dry or not exe:
            self.echo(f"--- prompt for {sub} (claude -p --model {model}){'' if exe else ': claude is not on PATH'} ---\n"
                      f"{prompt}\n--- end of prompt for {sub} ---")
            return None
        p = self.log(f"{sub}.prompt")
        p.write_text(prompt)
        with p.open() as stdin, self.log(f"{sub}.log").open("w") as out:
            return subprocess.Popen([exe, "-p", "--model", model, "--permission-mode", self.permission_mode(),
                                     "--output-format", "text"], stdin=stdin, stdout=out, stderr=subprocess.STDOUT,
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
        self.auto("start", f"interval {interval}s, cap ${self.c['max_usd_per_hour']}/h" + (", once" if once else ""),
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
            f"cost ${c.get('usd', 0):.2f} ({c.get('wall_s', 0):.0f}s on {c.get('lane', '?')}, {c.get('uncached_in', 0)} uncached / "
            f"{c.get('cache_read', 0)} cached / {c.get('out', 0)} out)." + (f" Note: {res['note']}." if res.get("note") else "")
            + (f" The spec's {res['verdict']} branch: {branch}" if branch else ""))
