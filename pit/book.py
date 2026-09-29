"""Pit: a prediction market that schedules runs (README: Pit).

Agents get a steady income, pay for the runs they post, and bet PASS/FAIL per variant. Wallets are never
stored: balance = drips + settlements - stakes - budgets, folded from rows by Book.
Rows: `agent` {id, kind: persistent|sub, parent, brief}, `drip` {since, until, minutes, usd, to: {agent: usd}},
`bet` {job, variant, side, usd, agent, book (the wallet), tags: auto|self}, `settle` {job, variant, outcome:
pass|fail|void, verdict, totals, pot, vig, payouts: {wallet: usd}}. A job node whose spec has `proposer` is the
budget debit. A sub has no wallet: its budgets and bets are booked to its persistent ancestor.
Who funds a post (v1.1): a root (no depends_on, no `from`) posted by `reflect` (or a sub of it) or with --seed is
staked by the house from its vig pool (`house_seed` per variant, capped by the pool; a `bet` row with agent `house`,
tagged `seed`) and debits no budget; a post `--as human` debits nothing and carries its --stake (tagged `human`);
any other agent pays its budget and the default stake from its wallet.
"""
from datetime import datetime, timedelta, timezone

from . import ledger as L

HOUSE = "house"
HUMAN = "human"
REFLECT = "reflect"
SIDES = ("pass", "fail")


def conf(cfg: dict) -> dict:
    return {"enabled": False, "default_stake": 0.25, "vig_rate": 0.02, "house_seed": 1.0, **cfg.get("pit", {})}


def enabled(cfg: dict) -> bool:
    return bool(conf(cfg)["enabled"])


def variants(spec: dict) -> list[str]:
    """A job is a task; its variants are its literal `arms` list, else the single variant "main"."""
    arms = spec.get("arms")
    return [str(a) for a in arms] if isinstance(arms, list) and arms else ["main"]


def iso(t: datetime) -> str:
    return t.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Book:
    def __init__(self, rows: list[dict]):
        self.agents: dict[str, dict] = {}
        self.bets: list[dict] = []                       # in ledger order
        self.settled: dict[tuple, dict] = {}             # (job, variant) -> settle row
        self.proposers: dict[str, str] = {}              # job -> agent
        self.flows: dict[str, float] = {}                # wallet -> usd
        self.last_drip: dict | None = None
        closed: set[str] = set()                         # betting closes at claim
        for r in rows:
            t = r["t"]
            if t == "agent":
                self.agents[r["id"]] = r
            elif t == "drip":
                self.last_drip = r
                for a, usd in r["to"].items():
                    self._add(a, usd)
            elif t == "node" and r.get("kind") == "job" and r["spec"].get("proposer"):
                self.proposers[r["id"]] = r["spec"]["proposer"]
                if not r["spec"].get("seed") and r["spec"]["proposer"] != HUMAN:   # seeds and human posts debit no wallet
                    self._add(self.wallet(r["spec"]["proposer"]), -r["spec"].get("budget_usd", 0))
            elif t == "claim":
                closed.add(r["job"])
            elif t == "bet" and r["job"] not in closed:
                self.bets.append(r)
                self._add(r["book"], -r["usd"])
            elif t == "settle" and (r["job"], r["variant"]) not in self.settled:
                self.settled[(r["job"], r["variant"])] = r
                for w, usd in r["payouts"].items():
                    self._add(w, usd)

    def _add(self, wallet, usd):
        self.flows[wallet] = round(self.flows.get(wallet, 0.0) + usd, 4)

    def wallet(self, agent: str) -> str:
        seen = set()
        while self.agents.get(agent, {}).get("kind") == "sub" and agent not in seen:
            seen.add(agent)
            agent = self.agents[agent]["parent"]
        return agent

    def family(self, agent: str) -> set[str]:
        """The agent and every sub below it (its straight line downward)."""
        out, grew = {agent}, True
        while grew:
            kids = {a for a, r in self.agents.items() if r.get("parent") in out}
            grew, out = bool(kids - out), out | kids
        return out

    def balance(self, agent: str) -> float:
        return self.flows.get(self.wallet(agent), 0.0)

    def balances(self) -> dict[str, float]:
        out = {a: self.balance(a) for a, r in self.agents.items() if r["kind"] == "persistent"}
        return {**out, **{w: self.flows[w] for w in (HOUSE, HUMAN) if w in self.flows}}

    def totals(self, job: str, variant: str) -> dict[str, float]:
        tot = {"pass": 0.0, "fail": 0.0}
        for b in self.bets:
            if b["job"] == job and b["variant"] == variant:
                tot[b["side"]] = round(tot[b["side"]] + b["usd"], 4)
        return tot

    def matched(self, job: str, spec: dict) -> float:
        return sum(2 * min(self.totals(job, v).values()) for v in variants(spec))


# ---- verbs ------------------------------------------------------------------------------------------

def agent_row(book: Book, aid: str, brief: str, parent: str | None = None) -> dict:
    if aid in book.agents or aid == HOUSE:
        raise SystemExit(f"agent {aid} already exists")
    if parent and parent not in book.agents:
        raise SystemExit(f"no agent {parent}")
    return {"t": "agent", "id": aid, "kind": "sub" if parent else "persistent", "parent": parent, "brief": brief}


def tick(ledger: L.Ledger, cfg: dict, now: datetime | None = None, since: str | None = None) -> dict | None:
    """Mint the lane rates for the whole minutes since the last drip (or `since` on the first tick), split
    evenly across persistent agents. Within the same minute it appends nothing."""
    book, now = Book(ledger.rows()), now or datetime.now(timezone.utc)
    last = parse_t(book.last_drip["until"]) if book.last_drip else parse_t(since) if since else now
    minutes = int((now - last).total_seconds() // 60)
    if minutes <= 0:
        return None
    usd = minutes * sum(l.get("usd_per_h", 0) for n, l in cfg["lanes"].items() if n != "any") / 60
    live = [a for a, r in book.agents.items() if r["kind"] == "persistent"]
    return ledger.append({"t": "drip", "since": iso(last), "until": iso(last + timedelta(minutes=minutes)),
                          "minutes": minutes, "usd": round(usd, 4),
                          "to": {a: round(usd / len(live), 4) for a in live}}, iso(now))


def bet_row(book: Book, st: L.State, job: str, variant: str, side: str, usd: float, agent: str,
            auto: bool = False) -> dict:
    """Refuses (SystemExit) a bet the market cannot take."""
    j = st.jobs.get(job)
    if not j:
        raise SystemExit(f"no job {job}")
    if j["state"] != "queued":
        raise SystemExit(f"{job} is {j['state']}: betting closed at claim")
    if variant not in variants(j["spec"]):
        raise SystemExit(f"{job} has no variant {variant} (have: {', '.join(variants(j['spec']))})")
    if side not in SIDES or usd <= 0:
        raise SystemExit("a bet is PASS or FAIL and a positive amount")
    if agent not in book.agents:
        raise SystemExit(f"no agent {agent} (q agent add)")
    wallet = book.wallet(agent)
    if book.balance(agent) < usd:
        raise SystemExit(f"{wallet} has ${book.balance(agent):.2f}, the bet is ${usd:.2f}")
    proposer = book.proposers.get(job) or j["spec"].get("proposer")
    tags = (["auto"] if auto else []) + (["self"] if proposer and book.wallet(proposer) == wallet else [])
    return {"t": "bet", "job": job, "variant": variant, "side": side, "usd": round(usd, 4), "agent": agent,
            "book": wallet, "tags": tags}


def funding(book: Book, spec: dict, agent: str, seed: bool = False) -> str:
    """"seed" (the house stakes a root from its vig), "human" (no wallet; --stake) or "agent" (its wallet pays)."""
    root = not spec.get("depends_on") and not spec.get("from")
    if root and (seed or book.wallet(agent) == REFLECT):
        return "seed"
    return "human" if agent == HUMAN else "agent"


def stakes(rows: list[dict], cfg: dict, spec: dict, agent: str, mode: str, stake: float = 0.0) -> list[dict]:
    """The bet rows a post opens the book with, per variant (append them after the job node)."""
    out = []
    for v in variants(spec):
        book = Book(rows + out)
        if mode == "seed":
            usd = round(min(conf(cfg)["house_seed"], book.flows.get(HOUSE, 0.0)), 4)
            if usd <= 0:
                break                     # the pool is empty: the root waits for a bettor
            out.append({"t": "bet", "job": spec["id"], "variant": v, "side": spec["expect"], "usd": usd,
                        "agent": HOUSE, "book": HOUSE, "tags": ["seed"]})
        elif mode == "human":
            if stake > 0:
                out.append({"t": "bet", "job": spec["id"], "variant": v, "side": spec["expect"], "usd": round(stake, 4),
                            "agent": HUMAN, "book": HUMAN, "tags": ["human"]})
        else:
            out.append(bet_row(book, L.fold(rows), spec["id"], v, spec["expect"], conf(cfg)["default_stake"], agent, auto=True))
    return out


def check_post(book: Book, cfg: dict, spec: dict, agent: str) -> str | None:
    if agent not in book.agents:
        return f"no agent {agent} (q agent add)"
    need = spec.get("budget_usd", 0) + conf(cfg)["default_stake"] * len(variants(spec))
    if book.balance(agent) < need:
        return f"{book.wallet(agent)} has ${book.balance(agent):.2f}; posting {spec.get('id')} needs ${need:.2f} " \
               f"(budget ${spec.get('budget_usd', 0)} + the default stake)"
    return None


# ---- sleep: an agent that cannot act yet hands the house a wake condition -------------------------------

WAKE_KEYS = ("balance", "result", "market", "minutes")


def sleep_row(book: Book, agent: str, until: dict, note: str) -> dict:
    until = {k: v for k, v in until.items() if k in WAKE_KEYS and v is not None}
    if agent not in book.agents:
        raise SystemExit(f"no agent {agent} (q agent add)")
    if not until:
        raise SystemExit("q sleep needs a condition: --until-balance, --until-result, --until-market or --minutes")
    return {"t": "sleep", "agent": agent, "until": until, "note": note}


def sleepers(rows: list[dict]) -> dict[str, dict]:
    """agent -> {sleep, i (its row index), wake (the wake row or None)}. Any claim, bet or post by the agent ends it."""
    out: dict[str, dict] = {}
    for i, r in enumerate(rows):
        actor = r.get("agent") if r["t"] in ("claim", "bet") else \
            r["spec"].get("proposer") if r["t"] == "node" and r.get("kind") == "job" else None
        if r["t"] == "sleep":
            out[r["agent"]] = {"sleep": r, "i": i, "wake": None}
        elif r["t"] == "wake" and r["agent"] in out:
            out[r["agent"]]["wake"] = r
        elif actor in out:
            del out[actor]
    return out


def until_text(u: dict) -> str:
    return ", ".join(filter(None, [u.get("balance") is not None and f"balance ${u['balance']:.2f}",
                                   u.get("result") and f"{u['result']} has a result",
                                   u.get("market") and f"{u['market']}'s market moves",
                                   u.get("minutes") and f"{u['minutes']} min"]))


def wake_reason(rows: list[dict], z: dict, now: datetime) -> str | None:
    u, book, st = z["sleep"]["until"], Book(rows), L.fold(rows)
    if "balance" in u and book.balance(z["sleep"]["agent"]) >= u["balance"]:
        return f"balance ${book.balance(z['sleep']['agent']):.2f} >= ${u['balance']:.2f}"
    if "result" in u and (st.jobs.get(u["result"]) or {}).get("result"):
        return f"{u['result']} ended {st.jobs[u['result']]['result']['verdict']}"
    if "market" in u and any(r["t"] == "bet" and r["job"] == u["market"] for r in rows[z["i"] + 1:]):
        t = {v: book.totals(u["market"], v) for v in variants(st.jobs[u["market"]]["spec"])} if u["market"] in st.jobs else {}
        return f"{u['market']}'s market moved: " + ", ".join(f"{v} PASS ${x['pass']:.2f} / FAIL ${x['fail']:.2f}" for v, x in t.items())
    if "minutes" in u and (now - parse_t(z["sleep"]["ts"])).total_seconds() >= 60 * u["minutes"]:
        return f"{u['minutes']} min passed"
    return None


def wake(ledger: L.Ledger, now: datetime | None = None) -> list[dict]:
    """Append a `wake` row for every sleeping agent whose condition holds (once per sleep); `q tick` runs it."""
    now, rows, out = now or datetime.now(timezone.utc), ledger.rows(), []
    for agent, z in sleepers(rows).items():
        why = None if z["wake"] else wake_reason(rows, z, now)
        if why:
            out.append(ledger.append({"t": "wake", "agent": agent, "reason": why}, iso(now)))
    return out


# ---- scoring ----------------------------------------------------------------------------------------

def rank(st: L.State, book: Book, ids: list[str] | None = None) -> tuple[list[str], set[str]]:
    """matched/budget_usd desc, ties cheapest first. In a lane where no runnable task has matched > 0 the
    cheapest one is the stall fallback (returned in the set, and marked on its claim row)."""
    ids = st.frontier() if ids is None else ids
    budget = {j: st.jobs[j]["spec"]["budget_usd"] for j in ids}
    matched = {j: book.matched(j, st.jobs[j]["spec"]) for j in ids}
    order = sorted(ids, key=lambda j: (-matched[j] / max(budget[j], 0.01), budget[j]))
    fallback = set()
    for lane in {st.jobs[j]["spec"]["lane"] for j in ids}:
        mine = [j for j in order if st.jobs[j]["spec"]["lane"] == lane]
        if not any(matched[j] > 0 for j in mine):
            fallback.add(mine[0])
    return order, fallback


def order(st: L.State, rows: list[dict], cfg: dict, ids: list[str] | None = None) -> tuple[list[str], set[str]]:
    """The ranking every caller uses: Pit's when [pit] enabled, else the old default."""
    return rank(st, Book(rows), ids) if enabled(cfg) else (L.rank(st, cfg, ids), set())


# ---- settlement -------------------------------------------------------------------------------------

def settle_due(ledger: L.Ledger, cfg: dict) -> list[dict]:
    """Append a settle row for every market whose task has a result, or was cancelled or superseded.
    pass/fail: winners split the pot less the vig pro rata (no winners: the house keeps it);
    invalid/unknown/cancelled/superseded: everyone is refunded, no vig. Idempotent."""
    rows = ledger.rows()
    st, book, out = L.fold(rows), Book(rows), []
    for key in dict.fromkeys((b["job"], b["variant"]) for b in book.bets):
        j = st.jobs.get(key[0])
        if key in book.settled or not j:
            continue
        if j["result"]:
            verdict = ((j["result"].get("result") or {}).get("verdicts") or {}).get(key[1], j["result"]["verdict"])
        elif j["state"] in ("cancelled", "superseded"):
            verdict = j["state"]
        else:
            continue
        bets = [b for b in book.bets if (b["job"], b["variant"]) == key]
        pot = round(sum(b["usd"] for b in bets), 4)
        pay: dict[str, float] = {}
        if verdict in SIDES:
            vig = round(conf(cfg)["vig_rate"] * pot, 4)
            won = sum(b["usd"] for b in bets if b["side"] == verdict)
            for b in bets:
                if b["side"] == verdict:
                    pay[b["book"]] = pay.get(b["book"], 0) + (pot - vig) * b["usd"] / won
            pay[HOUSE] = pay.get(HOUSE, 0) + (vig if won else pot)
        else:
            vig = 0.0
            for b in bets:
                pay[b["book"]] = pay.get(b["book"], 0) + b["usd"]
        out.append(ledger.append({"t": "settle", "job": key[0], "variant": key[1], "verdict": verdict,
                                  "outcome": verdict if verdict in SIDES else "void", "totals": book.totals(*key),
                                  "pot": pot, "vig": vig, "payouts": {w: round(u, 4) for w, u in pay.items()}},
                                 j["result"]["ts"] if j["result"] else None))
    return out


# ---- reading ----------------------------------------------------------------------------------------

def returned(bet: dict, s: dict) -> float:
    if s["outcome"] == "void":
        return bet["usd"]
    if bet["side"] != s["outcome"]:
        return 0.0
    return (s["pot"] - s["vig"]) * bet["usd"] / s["totals"][bet["side"]]


def calibration(rows: list[dict]) -> str:
    """Per wallet: non-self bets, wins, staked, returned, Brier from the stake share at close, herding
    (bet with the side that was ahead when it was placed). Self bets are only counted."""
    book, stats, running = Book(rows), {}, {}
    for b in book.bets:
        key = (b["job"], b["variant"])
        tot = running.setdefault(key, {"pass": 0.0, "fail": 0.0})
        other = "fail" if b["side"] == "pass" else "pass"
        herd = tot[b["side"]] > tot[other]
        tot[b["side"]] += b["usd"]
        if "seed" in b.get("tags", []):
            continue                      # the house's seed is not a forecast
        s = stats.setdefault(b["book"], {"bets": 0, "wins": 0, "staked": 0.0, "returned": 0.0, "brier": [],
                                         "herd": 0, "self": 0})
        if "self" in b.get("tags", []):
            s["self"] += 1
            continue
        s["bets"] += 1
        s["staked"] += b["usd"]
        s["herd"] += herd
        st = book.settled.get(key)
        if st:
            s["returned"] += returned(b, st)
            if st["outcome"] in SIDES:
                won = st["outcome"] == b["side"]
                s["wins"] += won
                s["brier"].append((st["totals"][b["side"]] / st["pot"] - won) ** 2)
    lines = [f"{'agent':<14} {'bets':>4} {'wins':>4} {'staked':>9} {'returned':>9} {'brier':>6} {'herding':>7} {'self':>4}"]
    for a in sorted(set(stats) | {x for x, r in book.agents.items() if r["kind"] == "persistent"}):
        s = stats.get(a, {"bets": 0, "wins": 0, "staked": 0.0, "returned": 0.0, "brier": [], "herd": 0, "self": 0})
        brier = f"{sum(s['brier']) / len(s['brier']):.3f}" if s["brier"] else "-"
        herd = f"{s['herd'] / s['bets']:.0%}" if s["bets"] else "-"
        lines.append(f"{a:<14} {s['bets']:>4} {s['wins']:>4} {s['staked']:>9.2f} {s['returned']:>9.2f} {brier:>6} "
                     f"{herd:>7} {s['self']:>4}")
    return "\n".join(lines)


def _cap(lines: list[str], n: int, what: str) -> list[str]:
    return lines if len(lines) <= n else lines[:n] + [f"  ... {len(lines) - n} more {what}"]


def thread(rows: list[dict], agent: str) -> str:
    """The context a spawned agent receives (<= 60 lines): brief, balance, its nodes in order with verdicts
    and findings, its open bets, and the open markets it has not bet on."""
    book, st = Book(rows), L.fold(rows)
    if agent not in book.agents:
        raise SystemExit(f"no agent {agent}")
    a, fam = book.agents[agent], book.family(agent)
    claimed = {r["job"] for r in rows if r["t"] == "claim" and r.get("agent") in fam}
    out = [f"{agent} ({a['kind']}{', under ' + a['parent'] if a.get('parent') else ''}) · balance "
           f"${book.balance(agent):.2f}" + (f" (wallet {book.wallet(agent)})" if book.wallet(agent) != agent else ""),
           f"brief: {a['brief']}"]
    z = sleepers(rows).get(agent)
    if z:
        out.append(f"awake: {z['wake']['reason']}" if z["wake"] else
                   f"sleeping until {until_text(z['sleep']['until'])} (since {z['sleep']['ts'][11:16]}Z): {z['sleep']['note']}")
    nodes = []
    for jid in sorted((j for j in st.jobs if book.proposers.get(j) in fam or j in claimed), key=lambda j: st.jobs[j]["added"]):
        j = st.jobs[jid]
        v = f" {j['result']['verdict']}" if j["result"] else ""
        nodes.append(f"  {jid} [{j['state']}{v}] ${j['spec']['budget_usd']} {j['spec']['question'][:80]}")
        nodes += [f"    -> {fid}: {f['text'][:90]}" for fid, f in st.findings.items() if f["from"] == jid]
    out += ["nodes:"] + (_cap(nodes[::-1], 20, "earlier lines")[::-1] if nodes else ["  none yet"])
    mine = [b for b in book.bets if b["agent"] in fam and (b["job"], b["variant"]) not in book.settled]
    bets = [f"  {b['job']}/{b['variant']} {b['side'].upper()} ${b['usd']:.2f}  (book PASS ${book.totals(b['job'], b['variant'])['pass']:.2f}"
            f" / FAIL ${book.totals(b['job'], b['variant'])['fail']:.2f})" for b in mine]
    out += ["open bets:"] + (_cap(bets, 10, "bets") or ["  none"])
    have = {(b["job"], b["variant"]) for b in book.bets if b["agent"] in fam}
    markets = []
    for jid, j in st.jobs.items():
        if j["state"] != "queued":
            continue
        for v in variants(j["spec"]):
            if (jid, v) not in have:
                t = book.totals(jid, v)
                markets.append(f"  {jid}/{v} PASS ${t['pass']:.2f} / FAIL ${t['fail']:.2f} · ${j['spec']['budget_usd']} "
                               f"{j['spec']['lane']} · {j['spec']['question'][:60]}")
    out += ["open markets you have not bet on:"] + (_cap(markets, 20, "markets") or ["  none"])
    return "\n".join(out)
