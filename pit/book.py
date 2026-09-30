"""Pit: a prediction market that schedules runs (README: Pit).

Agents get a steady income, pay for the runs they post, and bet PASS/FAIL per variant. Wallets are never
stored: balance = drips + settlements - stakes - budgets + funding, folded from rows by Book. The market pot is
stakes only; funding is a separate pool: a post escrows its budget_usd, and its result row's `funding` {wallet, usd}
books the unspent part back (+) or the overage past it (-, as far as the wallet goes; `shortfall` names the rest).
Rows: `agent` {id, kind: persistent|sub, parent, brief, by?, runtime?, model?} (a later row for the same id replaces it), `retire` {agent, reason, by}, `drip` {since, until, minutes, usd, to: {agent: usd}},
`bet` {job, variant, side, usd, agent, book (the wallet), tags: auto|self}, `settle` {job, variant, outcome:
pass|fail|void, verdict, totals, pot, vig, payouts: {wallet: usd}}. A job node whose spec has `proposer` is the
budget debit. A sub has no wallet: its budgets and bets are booked to its persistent ancestor.
Who funds a post (v1.1): a root (no depends_on, no `from`) posted by `reflect` (or a sub of it) or with --seed is
staked by the house from its vig pool (`house_seed` per variant, capped by the pool; a `bet` row with agent `house`,
tagged `seed`) and debits no budget; a post `--as human` debits nothing and carries its --stake (tagged `human`);
any other agent pays its budget and the auto stake (max(default_stake, stake_share x budget_usd)) from its wallet.
A read (`kind = "read"`, spec.is_read) is funded like any post but has no market: no seed, no stake, no bets, no settle;
it is never counted in a record, and ranks by cost among the zero-matched jobs (it can have no matched stakes).
"""
import re
import statistics
from datetime import datetime, timedelta, timezone

from . import ledger as L, spec as specmod

HOUSE = "house"
HUMAN = "human"
REFLECT = "reflect"
SIDES = ("pass", "fail")


def conf(cfg: dict) -> dict:
    return {"enabled": False, "default_stake": 0.25, "vig_rate": 0.02, "house_seed": 1.0, "rank": "matched", "mint": 1.0,
            "max_posts_per_hour": 0, "stake_share": 0.25, "blind": True, **cfg.get("pit", {})}


def blind(cfg: dict) -> bool:
    """[pit] blind (default true): what an agent sees carries nothing about others' bets (no pools, odds, backers or
    whys), only job, lane, claim, question, funding, proposer and record. The human terminal and ranking see everything."""
    return bool(conf(cfg)["blind"])


def funded(spec: dict, cfg: dict) -> str:
    return f"funded ${spec.get('budget_usd', 0):g} ({specmod.funded_seconds(spec, cfg.get('lanes', {}))}s)"


def scenario_of(spec: dict, cfg: dict | None = None) -> str | None:
    """The spec's scenario, else the {scenario} its run fills into its lane's runner template (a hand-written runner call)."""
    if spec.get("scenario"):
        return spec["scenario"]
    tpl = ((cfg or {}).get("lanes", {}).get(spec.get("lane"), {})).get("runner")
    if not tpl or "{scenario}" not in tpl or not spec.get("run"):
        return None
    pat = re.sub(r"\\\{(\w+)\\\}", lambda m: r"(?P<scenario>\S+)" if m[1] == "scenario" else r"\S+", re.escape(tpl))
    m = re.search(pat, spec["run"])
    return m["scenario"].strip("'\"") if m else None


def typical_costs(rows: list[dict], cfg: dict | None = None) -> dict[tuple[str, str], dict]:
    """(scenario, lane) -> {n, wall_s, usd, kill} from the settled result of every job with a scenario: medians of wall_s
    and usd over its non-invalid results not killed for funding (None when all were), kill = the share killed (over budget)."""
    specs = {r["id"]: r["spec"] for r in rows if r["t"] == "node" and r.get("kind") == "job"}
    last = {r["job"]: r for r in rows if r["t"] == "result" and r["job"] in specs}      # a corrected result replaces the first
    runs: dict[tuple[str, str], list[dict]] = {}
    for jid, r in last.items():
        sc = scenario_of(specs[jid], cfg)
        if sc and r["verdict"] != "invalid":
            runs.setdefault((sc, r["cost"].get("lane") or specs[jid]["lane"]), []).append(r)
    killed = lambda r: r.get("note", "").startswith("over budget")
    out = {}
    for k, rs in runs.items():
        ok = [r for r in rs if not killed(r)]      # a killed run's wall stops at its funding: it would bias the typical cost low
        med = lambda f: statistics.median(r["cost"][f] for r in ok) if ok else None
        out[k] = {"n": len(rs), "wall_s": med("wall_s"), "usd": med("usd"), "kill": (len(rs) - len(ok)) / len(rs)}
    return out


def typical_cost(rows: list[dict], scenario: str, lane: str, cfg: dict | None = None) -> dict | None:
    return typical_costs(rows, cfg).get((scenario, lane))


def typical_here(spec: dict, cfg: dict, typ: dict) -> str:
    """' · typical here $Y (Ms)' for the spec's scenario on its lane, or '' when no run of it has settled there."""
    t = typ.get((scenario_of(spec, cfg), spec.get("lane")))
    return f" · typical here ${t['usd']:.2f} ({t['wall_s']:.0f}s)" if t and t["usd"] is not None else ""


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
        self.funds: dict[str, tuple[str, float]] = {}    # job -> (wallet, usd) booked at result: + refund, - overage
        self.retired: dict[str, dict] = {}               # agent -> its retire row: on the book, no wakes, no drip
        closed: set[str] = set()                         # betting closes at claim
        for r in rows:
            t = r["t"]
            if t == "agent":
                self.agents[r["id"]] = r
            elif t == "retire":
                self.retired[r["agent"]] = r
            elif t == "drip":
                self.last_drip = r
                for a, usd in r["to"].items():
                    self._add(a, usd)
            elif t == "node" and r.get("kind") == "job" and r["spec"].get("proposer"):
                self.proposers[r["id"]] = r["spec"]["proposer"]
                if self.payer(r["spec"]):
                    self._add(self.payer(r["spec"]), -r["spec"].get("budget_usd", 0))
            elif t == "result" and r.get("funding"):
                self.funds[r["job"]] = (r["funding"]["wallet"], r["funding"]["usd"])     # a corrected result replaces the earlier one
            elif t == "claim":
                closed.add(r["job"])
            elif t == "bet" and r["job"] not in closed:
                self.bets.append(r)
                self._add(r["book"], -r["usd"])
            elif t == "settle" and ((r["job"], r["variant"]) not in self.settled or r.get("supersedes")):
                self.settled[(r["job"], r["variant"])] = r     # a re-settle (verdict corrected) replaces the one it supersedes
                for w, usd in r.get("clawback", {}).items():
                    self._add(w, -usd)
                for w, usd in r["payouts"].items():
                    self._add(w, usd)
        for w, usd in self.funds.values():
            self._add(w, usd)

    def active(self) -> list[str]:
        """Persistent agents that still get income and turns: every one on the book that is not retired."""
        return [a for a, r in self.agents.items() if r["kind"] == "persistent" and a not in self.retired]

    def payer(self, spec: dict) -> str | None:
        """The wallet a post's funding came from; seeds, bag draws and human posts debit none."""
        p = spec.get("proposer")
        return None if not p or spec.get("seed") or spec.get("bag") or p == HUMAN else self.wallet(p)

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

RUNTIMES = ("claude", "codex")      # what an agent's turns are spawned on (pit.autopilot.Autopilot.argv)


def runtime_fields(runtime: str | None, model: str | None) -> dict:
    if runtime and runtime not in RUNTIMES:
        raise SystemExit(f"unknown runtime {runtime} (one of {', '.join(RUNTIMES)})")
    return {**({"runtime": runtime} if runtime else {}), **({"model": model} if model else {})}


def agent_row(book: Book, aid: str, brief: str, parent: str | None = None, runtime: str | None = None,
              model: str | None = None) -> dict:
    if aid in book.agents or aid == HOUSE:
        raise SystemExit(f"agent {aid} already exists")
    if parent and parent not in book.agents:
        raise SystemExit(f"no agent {parent}")
    return {"t": "agent", "id": aid, "kind": "sub" if parent else "persistent", "parent": parent, "brief": brief,
            **runtime_fields(runtime, model)}


def agent_set_row(book: Book, aid: str, runtime: str | None = None, model: str | None = None) -> dict:
    """A new row for an existing agent with its runtime/model changed; the latest row wins (Book keeps the last)."""
    if aid not in book.agents:
        raise SystemExit(f"no agent {aid}")
    old = {k: v for k, v in book.agents[aid].items() if k not in ("ts", "host")}
    if runtime and runtime != old.get("runtime", "claude"):
        old.pop("model", None)             # a model names one runtime's model; a runtime switch drops it
    return {**old, **runtime_fields(runtime, model)}


def retire_row(book: Book, aid: str, reason: str, by: str | None = None) -> dict:
    """Retire a persistent agent: it stays on the book (its record and balance), but gets no more wakes or drip."""
    if aid not in book.agents or book.agents[aid]["kind"] != "persistent":
        raise SystemExit(f"no persistent agent {aid}")
    if aid in book.retired or aid == REFLECT:
        raise SystemExit(f"{aid} is already retired" if aid in book.retired else "reflect is structural: it is never retired")
    return {"t": "retire", "agent": aid, "reason": reason, **({"by": by} if by else {})}


def tick(ledger: L.Ledger, cfg: dict, now: datetime | None = None, since: str | None = None) -> dict | None:
    """Mint the lane rates for the whole minutes since the last drip (or `since` on the first tick), split
    evenly across persistent agents. Within the same minute it appends nothing."""
    book, now = Book(ledger.rows()), now or datetime.now(timezone.utc)
    last = parse_t(book.last_drip["until"]) if book.last_drip else parse_t(since) if since else now
    minutes = int((now - last).total_seconds() // 60)
    if minutes <= 0:
        return None
    mint = conf(cfg)["mint"]       # share of lane capacity minted per minute; 1.0 = every lane sold every minute
    usd = mint * minutes * sum(l.get("usd_per_h", 0) for n, l in cfg["lanes"].items() if n != "any") / 60
    live = book.active()
    return ledger.append({"t": "drip", "since": iso(last), "until": iso(last + timedelta(minutes=minutes)),
                          "minutes": minutes, "usd": round(usd, 4),
                          "to": {a: round(usd / len(live), 4) for a in live}}, iso(now))


def bet_row(book: Book, st: L.State, job: str, variant: str, side: str, usd: float, agent: str,
            auto: bool = False, why: str | None = None) -> dict:
    """Refuses (SystemExit) a bet the market cannot take."""
    j = st.jobs.get(job)
    if not j:
        raise SystemExit(f"no job {job}")
    if specmod.is_read(j["spec"]):
        raise SystemExit(f"{job} is a read: reads have no market")
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
            "book": wallet, "tags": tags, **({"why": " ".join(why.split())} if why else {})}


def funding(book: Book, spec: dict, agent: str, seed: bool = False) -> str:
    """"seed" (the house stakes a root from its vig), "human" (no wallet; --stake) or "agent" (its wallet pays)."""
    root = not spec.get("depends_on") and not spec.get("from") and not specmod.is_read(spec)     # a read is never seeded
    if root and (seed or book.wallet(agent) == REFLECT):
        return "seed"
    return "human" if agent == HUMAN else "agent"


def auto_stake(cfg: dict, spec: dict) -> float:
    """The stake a post puts on PASS, per variant: a share of its funding, default_stake the floor."""
    c = conf(cfg)
    return round(max(c["default_stake"], c["stake_share"] * spec.get("budget_usd", 0)), 4)


def stakes(rows: list[dict], cfg: dict, spec: dict, agent: str, mode: str, stake: float = 0.0) -> list[dict]:
    """The bet rows a post opens the book with, per variant (append them after the job node)."""
    out = []
    for v in [] if specmod.is_read(spec) else variants(spec):
        book = Book(rows + out)
        if mode == "seed":
            seed = cfg.get("lanes", {}).get(spec.get("lane"), {}).get("house_seed", conf(cfg)["house_seed"])
            usd = round(min(seed, book.flows.get(HOUSE, 0.0)), 4)
            if usd <= 0:
                break                     # the pool is empty: the root waits for a bettor
            out.append({"t": "bet", "job": spec["id"], "variant": v, "side": "pass", "usd": usd,
                        "agent": HOUSE, "book": HOUSE, "tags": ["seed"]})
        elif mode == "human":
            if stake > 0:
                out.append({"t": "bet", "job": spec["id"], "variant": v, "side": "pass", "usd": round(stake, 4),
                            "agent": HUMAN, "book": HUMAN, "tags": ["human"]})
        else:
            out.append(bet_row(book, L.fold(rows), spec["id"], v, "pass", auto_stake(cfg, spec), agent, auto=True))
    return out


def funding_row(rows: list[dict], spec: dict, cost_usd: float) -> dict:
    """The `funding` of a result row: the unspent budget_usd back to the payer (+), or the overage past it (-) as far as
    the wallet goes (`shortfall` names the rest). {} when no wallet paid (seeds, bag draws, human posts)."""
    book = Book(rows)
    w = book.payer(spec)
    if not w:
        return {}
    usd = round(spec.get("budget_usd", 0) - cost_usd, 4)
    if usd >= 0:
        return {"wallet": w, "usd": usd}
    have = max(0.0, book.balance(w) - book.funds.get(spec["id"], (w, 0.0))[1])     # a re-record replaces its own earlier row
    take = round(min(-usd, have), 4)
    short = round(-usd - take, 4)
    return {"wallet": w, "usd": -take, **({"shortfall": short} if short else {})}


def check_post(book: Book, cfg: dict, spec: dict, agent: str, rows: list[dict] | None = None) -> str | None:
    if agent not in book.agents:
        return f"no agent {agent} (q agent add)"
    read = specmod.is_read(spec)
    need = spec.get("budget_usd", 0) + (0 if read else auto_stake(cfg, spec) * len(variants(spec)))
    if book.balance(agent) < need:
        return f"{book.wallet(agent)} has ${book.balance(agent):.2f}; posting {spec.get('id')} needs ${need:.2f} " + \
            (f"(a read: its budget only)" if read else f"(budget ${spec.get('budget_usd', 0)} + the auto stake ${auto_stake(cfg, spec):.2f} per variant)")
    cap = conf(cfg)["max_posts_per_hour"]
    w = book.wallet(agent)
    if w != "house" and cap:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        n = sum(1 for r in (rows or []) if r.get("t") == "node" and r.get("kind") == "job"
                and book.wallet(r.get("spec", {}).get("proposer") or "") == w and r.get("ts", "") >= cutoff)
        if n >= cap:
            return f"{w} has posted {n} jobs in the last hour (cap {cap}): record a finding or sleep instead of posting"
    return None


# ---- sleep: an agent that cannot act yet hands the house a wake condition -------------------------------

WAKE_KEYS = ("balance", "result", "market", "minutes", "event")


def sleep_row(book: Book, agent: str, until: dict, note: str) -> dict:
    until = {k: v for k, v in until.items() if k in WAKE_KEYS and v is not None}
    if agent not in book.agents:
        raise SystemExit(f"no agent {agent} (q agent add)")
    if not until:
        raise SystemExit("q sleep needs a condition: --until-event, --until-balance, --until-result, --until-market or --minutes")
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


def explicit_sleep(rows: list[dict], book: Book, agent: str) -> dict | None:
    """The agent's (or its subs') standing sleep row with an explicit condition, else None. An `event` sleep, a
    wake, or any claim/bet/post by the family clears it: that agent is event-driven (autopilot wakes it on board events)."""
    fam, cur = book.family(agent), None
    for r in rows:
        actor = r.get("agent") if r["t"] in ("claim", "bet", "sleep", "wake") else \
            r["spec"].get("proposer") if r["t"] == "node" and r.get("kind") == "job" else None
        if actor in fam:
            cur = r if r["t"] == "sleep" and "event" not in r["until"] else None
    return cur


def board_events(rows: list[dict], book: Book, fam: set[str], since: int, bets: bool = True) -> list[int]:
    """Row indexes >= since that are board events to `fam`: a new job or finding node, a result, a settle, or a
    (non-seed) bet. Its own posts, bets, findings and its own jobs' results are not events (hand-backs cover those)."""
    bagjobs = {r["id"] for r in rows if r["t"] == "node" and r.get("kind") == "job" and r["spec"].get("bag")}
    out = []
    for i in range(since, len(rows)):
        r = rows[i]
        mine = (r["spec"].get("proposer") if r.get("kind") == "job" else r.get("agent")) if r["t"] == "node" else \
            book.proposers.get(r["job"]) if r["t"] == "result" else r.get("agent") if r["t"] == "bet" else None
        if r["t"] not in ("node", "result", "settle", "bet") or r["t"] == "bet" and not bets or mine in fam or {"seed", "bag"} & set(r.get("tags", [])):
            continue
        if r["t"] == "result" and (r["verdict"] == "invalid" or r["job"] in bagjobs and r["verdict"] not in SIDES):
            continue      # invalid is noise; a bag result counts only as pass/fail
        if r["t"] == "node" and r.get("kind") == "job" and r["spec"].get("bag"):
            continue      # a bag post
        if r["t"] == "settle" and r["job"] in bagjobs and r.get("outcome") not in SIDES:
            continue
        out.append(i)
    return out


def digest(rows: list[dict], events: list[int], cfg: dict | None = None) -> str:
    """'Since you last looked' (<= 30 lines): new markets with PASS/FAIL totals, moved markets, results, settlements, findings.
    With a blind cfg: new markets show funding instead of totals, and moved markets are left out."""
    hide = cfg is not None and blind(cfg)
    book, st, lines = Book(rows), L.fold(rows), {}
    for i in events:
        r = rows[i]
        if r["t"] == "node" and r.get("kind") == "job" and specmod.is_read(r["spec"]):
            lines[f"m{r['id']}"] = f"new read {r['id']} {funded(r['spec'], cfg or {})} {r['spec'].get('lane')} · {specmod.claim_first(r['spec'], 60)}"
        elif r["t"] == "node" and r.get("kind") == "job":
            for v in variants(r["spec"]):
                t = book.totals(r["id"], v)
                lines[f"m{r['id']}/{v}"] = (f"new market {r['id']}/{v}{specmod.change_mark(r['spec'])}{' [bag]' if r['spec'].get('bag') else ''} "
                                            + (f"{funded(r['spec'], cfg)} {r['spec'].get('lane')}" if hide else
                                               f"PASS ${t['pass']:.2f} / FAIL ${t['fail']:.2f} · ${r['spec'].get('budget_usd', 0)} {r['spec'].get('lane')}")
                                            + f" · {specmod.claim_first(r['spec'], 60)}")
        elif r["t"] == "node":
            lines[f"f{r['id']}"] = f"finding {r['id']}: {r.get('text', '')[:80]}"
        elif r["t"] == "result":
            lines[f"r{r['job']}"] = f"result {r['job']}: {r['verdict']}"
        elif r["t"] == "settle":
            lines[f"s{r['job']}/{r['variant']}"] = f"settled {r['job']}/{r['variant']}: {r['outcome']}"
        elif r["t"] == "bet" and not hide and f"m{r['job']}/{r['variant']}" not in lines:
            t = book.totals(r["job"], r["variant"])
            lines[f"b{r['job']}/{r['variant']}"] = f"market moved {r['job']}/{r['variant']}: PASS ${t['pass']:.2f} / FAIL ${t['fail']:.2f}"
    refs = refuters(rows)
    return "\n".join(mark_refuted(x, refs) for x in _cap(list(lines.values()), 30, "events"))


def refuters(rows: list[dict]) -> dict[str, str]:
    """finding id -> who refuted it (the refuting finding's agent, or the refuting job's proposer), from `refutes` edges."""
    who = {r["id"]: r.get("agent") or (r.get("spec") or {}).get("proposer") for r in rows if r["t"] == "node"}
    fids = {r["id"] for r in rows if r["t"] == "node" and r.get("kind") != "job"}
    return {r["to"]: who.get(r["from"]) or r["from"] for r in rows if r["t"] == "edge" and r["type"] == "refutes" and r["to"] in fids}


def mark_refuted(text: str, refs: dict[str, str]) -> str:
    """Every citation of a refuted finding in `text` gets '(refuted by <agent>)' after it, so a wrong idea does not spread unmarked."""
    for fid, by in refs.items():
        text = re.sub(rf"(?<![\w:-]){re.escape(fid)}(?![\w-])(?! \(refuted)", lambda m: f"{m[0]} (refuted by {by})", text)
    return text


LOSS_RULE = ("Address each loss in your first finding this turn: what you believed, what the result showed, what you now "
             "expect; a loss you do not address is a wasted turn.")


def settled_stakes(rows: list[dict], agent: str, since: int) -> str:
    """'Your stakes that settled since your last turn': one line per bet of the agent's wallet (its subs' too) on a
    market settled at row >= since, with the why it gave and what it won or lost."""
    book = Book(rows)
    keys = dict.fromkeys((r["job"], r["variant"]) for r in rows[since:] if r["t"] == "settle")
    out = []
    for k in keys:
        s = book.settled[k]
        for b in (b for b in book.bets if (b["job"], b["variant"]) == k and b["book"] == book.wallet(agent)):
            if s["outcome"] == "void" or (s.get("void_self") and "self" in b["tags"]):
                how = "void"
            elif b["side"] == s["outcome"]:
                how = f"won +${max(returned(b, s) - b['usd'], 0):.2f}"
            else:
                how = f"lost ${b['usd']:.2f}"
            said = f' (you said: "{b["why"]}")' if b.get("why") else ""
            out.append(f"{k[0]} {k[1]}: you had {b['side'].upper()} ${b['usd']:.2f}{said} — {how}")
    return "Your stakes that settled since your last turn:\n" + "\n".join(_cap(out, 15, "stakes")) + "\n" + LOSS_RULE if out else ""


def reflection_since(rows: list[dict], since: int) -> str:
    """'Reflection since your last turn': reflect's findings (up to the newest 5) and any agents/BOOTSTRAP.md edit, once."""
    book, refs, win = Book(rows), refuters(rows), rows[since:]
    fs = [r for r in win if r["t"] == "node" and r.get("kind") == "finding" and book.wallet(r.get("agent") or "") == REFLECT][-5:]
    out = [mark_refuted(f"{r['id']}: {' '.join(r.get('text', '').split())[:200]}", refs) for r in fs]
    out += [f"agents/BOOTSTRAP.md edited ({r.get('job') or 'by hand'}): " + "; ".join([f"+ {x}" for x in r.get("add", [])] + [f"- {x}" for x in r.get("remove", [])])
            for r in win if r["t"] == "bootstrap"]
    return "Reflection since your last turn:\n" + "\n".join(out) if out else ""


NEW_RULE = ("For each: bet (`q bet <job> PASS|FAIL <usd> --as <you> --why '…'`) or write `pass: <one-line reason>` in your "
            "findings. Skipping one silently is a wasted turn.")


def new_markets(rows: list[dict], cfg: dict, agent: str, since: int, n: int = 10) -> str:
    """'New markets since your last turn': every open market (job x variant) posted at row >= since by another wallet
    that the agent's family has not bet on, newest first, with its funding and the typical cost of that scenario on that
    lane, its pools and what $1 on the thin side pays (not under blind), and the proposer."""
    book, st = Book(rows), L.fold(rows)
    fam, recs, out, typ = book.family(agent), records(book), [], typical_costs(rows, cfg)
    have = {(b["job"], b["variant"]) for b in book.bets if b["agent"] in fam}
    for r in reversed(rows[since:]):
        j = st.jobs.get(r.get("id")) if r["t"] == "node" and r.get("kind") == "job" else None
        if not j or j["state"] != "queued" or specmod.is_read(j["spec"]):
            continue
        s = j["spec"]
        prop = book.wallet(book.proposers.get(r["id"]) or s.get("proposer") or HUMAN)
        if prop == book.wallet(agent):
            continue
        for v in variants(s):
            if (r["id"], v) in have:
                continue
            t = book.totals(r["id"], v)
            side, x = pays(book, r["id"], v, "pass", cfg)
            out.append(f"{r['id'] if v == 'main' else r['id'] + '/' + v}{specmod.change_mark(s)} [{s['lane']}] {specmod.claim_first(s, 100)} · "
                       + f"{funded(s, cfg)}{typical_here(s, cfg, typ)} · "
                       + ("" if blind(cfg) else f"PASS ${t['pass']:.2f} / FAIL ${t['fail']:.2f} · $1 on {side.upper()} pays ${x:.2f} · ")
                       + f"proposer {prop} ({record(recs, prop)})")
    if not out:
        return ""
    more = [f"… {len(out) - n} more: q board"] if len(out) > n else []
    return "New markets since your last turn:\n" + "\n".join(out[:n] + more) + "\n" + NEW_RULE


def until_text(u: dict) -> str:
    return ", ".join(filter(None, [u.get("event") and "the next board event", u.get("balance") is not None and f"balance ${u['balance']:.2f}",
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

def rank(st: L.State, book: Book, ids: list[str] | None = None, mode: str = "matched") -> tuple[list[str], set[str]]:
    """[pit] rank: "matched" (default) = the most uncertain runs, matched stakes desc, ties cheapest first, then oldest;
    "matched_per_usd" = matched / budget_usd desc, then cheapest, then oldest. In a lane where no runnable task has
    matched > 0 the cheapest one is the stall fallback (returned in the set, and marked on its claim row)."""
    ids = st.frontier() if ids is None else ids
    budget = {j: st.jobs[j]["spec"]["budget_usd"] for j in ids}
    matched = {j: book.matched(j, st.jobs[j]["spec"]) for j in ids}
    score = (lambda j: matched[j] / max(budget[j], 0.01)) if mode == "matched_per_usd" else (lambda j: matched[j])
    order = sorted(ids, key=lambda j: (-score(j), budget[j], st.jobs[j]["added"]))
    fallback = set()
    for lane in {st.jobs[j]["spec"]["lane"] for j in ids}:
        mine = [j for j in order if st.jobs[j]["spec"]["lane"] == lane]
        if not any(matched[j] > 0 for j in mine):
            fallback.add(mine[0])
    return order, fallback


def order(st: L.State, rows: list[dict], cfg: dict, ids: list[str] | None = None) -> tuple[list[str], set[str]]:
    """The ranking every caller uses: Pit's when [pit] enabled, else the old default."""
    return rank(st, Book(rows), ids, conf(cfg)["rank"]) if enabled(cfg) else (L.rank(st, cfg, ids), set())


# ---- settlement -------------------------------------------------------------------------------------

def settle_due(ledger: L.Ledger, cfg: dict) -> list[dict]:
    """Append a settle row for every market whose task has a result, or was cancelled or superseded.
    pass/fail: winners split the pot less the vig pro rata (no winners: the house keeps it);
    invalid/unknown/cancelled/superseded: everyone is refunded, no vig. Idempotent.
    A pass/fail market whose newest verdict flips to the other side is re-settled: the new row claws back what the
    prior settle credited (the house keeps its vig), pays the new winners from the same pot less the same vig, and
    names the prior row in `supersedes`. The vig is charged once."""
    rows = ledger.rows()
    st, book, out = L.fold(rows), Book(rows), []
    for key in dict.fromkeys((b["job"], b["variant"]) for b in book.bets):
        j = st.jobs.get(key[0])
        prior = book.settled.get(key)
        if not j:
            continue
        if j["result"]:
            verdict = ((j["result"].get("result") or {}).get("verdicts") or {}).get(key[1], j["result"]["verdict"])
        elif j["state"] in ("cancelled", "superseded"):
            verdict = j["state"]
        else:
            continue
        # ponytail: only pass<->fail flips re-settle; a flip to/from void would need the vig refunded too
        if prior and not (prior["outcome"] in SIDES and verdict in SIDES and verdict != prior["outcome"]):
            continue
        allbets = [b for b in book.bets if (b["job"], b["variant"]) == key]
        res, spec = j["result"], j["spec"]
        # a desk job its proposer settles itself for $0: the proposer's own bets are refunded, not paid
        selfvoid = bool(res and verdict in SIDES and not spec.get("run") and not spec.get("scenario")
                        and res.get("agent") and res["agent"] == book.wallet(book.proposers.get(key[0], ""))
                        and not res["cost"]["usd"])
        bets = [b for b in allbets if not (selfvoid and "self" in b["tags"])]
        pot = round(sum(b["usd"] for b in bets), 4)
        pay: dict[str, float] = {b["book"]: b["usd"] for b in allbets if b not in bets}
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
        extra = {}
        if prior:       # claw back exactly what the prior row credited, except the vig the house already holds
            back = dict(prior["payouts"])
            if not prior.get("supersedes"):
                back[HOUSE] = back.get(HOUSE, 0) - prior["vig"]
            pay[HOUSE] = pay.get(HOUSE, 0) - vig
            extra = {"supersedes": prior["ts"], "reason": f"verdict corrected {prior['outcome']} -> {verdict}",
                     "clawback": {w: round(u, 4) for w, u in back.items() if round(u, 4)}}
            pay = {w: u for w, u in pay.items() if round(u, 4)}
        out.append(ledger.append({"t": "settle", "job": key[0], "variant": key[1], "verdict": verdict,
                                  "outcome": verdict if verdict in SIDES else "void", "totals": {s: round(sum(b["usd"] for b in bets if b["side"] == s), 4) for s in SIDES},
                                  "pot": pot, "vig": vig, **({"void_self": True} if selfvoid else {}), **extra, "payouts": {w: round(u, 4) for w, u in pay.items()}},
                                 j["result"]["ts"] if j["result"] else None))
    return out


# ---- reading ----------------------------------------------------------------------------------------

def claims(rows: list[dict], agent: str | None = None, since: datetime | None = None) -> str:
    """Every posted claim, oldest first, with its latest result. Fold once, without building the graph.
    Filter by the literal proposer id and posting timestamp; ledger rows already arrive in time order."""
    posts, verdicts = {}, {}
    for r in rows:
        if r["t"] == "node" and r.get("kind") == "job" and r["spec"].get("claim", "").strip():
            posts[r["id"]] = r
        elif r["t"] == "result":
            verdicts[r["job"]] = r["verdict"]
    out = []
    for jid, r in posts.items():
        s = r["spec"]
        proposer = s.get("proposer") or HUMAN
        if agent is not None and proposer != agent or since is not None and parse_t(r["ts"]) < since:
            continue
        out.append(f"{r['ts']} {verdicts.get(jid, 'open')} {proposer} {s['lane']} {jid} · "
                   + " ".join(s["claim"].split()))
    return "\n".join(out)


def returned(bet: dict, s: dict) -> float:
    if s["outcome"] == "void" or (s.get("void_self") and "self" in bet["tags"]):
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
        if {"seed", "bag"} & set(b.get("tags", [])):
            continue                      # the house's seed and bag stakes are not forecasts
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


def records(book: Book) -> dict[str, list[int]]:
    """wallet -> [post wins, post losses, bet wins, bet losses] over settled pass/fail markets. A post wins when it
    passes (a post is a claim that it will); a bet counts once per market and side, `self` bets excluded."""
    out: dict[str, list[int]] = {}
    for (job, v), s in book.settled.items():
        if s["outcome"] in SIDES and job in book.proposers:
            out.setdefault(book.wallet(book.proposers[job]), [0, 0, 0, 0])[0 if s["outcome"] == "pass" else 1] += 1
    for b in {(b["book"], b["side"], b["job"], b["variant"]) for b in book.bets
              if not {"self", "seed", "bag"} & set(b.get("tags", []))}:
        s = book.settled.get((b[2], b[3]))
        if s and s["outcome"] in SIDES:
            out.setdefault(b[0], [0, 0, 0, 0])[2 if b[1] == s["outcome"] else 3] += 1
    return out


def record(recs: dict, wallet: str) -> str:
    pw, pl, bw, bl = recs.get(wallet, [0, 0, 0, 0])
    return f"{pw}-{pl} on posts, {bw}-{bl} on bets"


def pays(book: Book, job: str, variant: str, other: str, cfg: dict) -> tuple[str, float]:
    """The thinner side (ties: the side against `other`, the proposer's) and what $1 placed there now returns per $1."""
    t = book.totals(job, variant)
    thin = min(SIDES, key=lambda s: (t[s], s == other))
    return thin, (t["pass"] + t["fail"] + 1) * (1 - conf(cfg)["vig_rate"]) / (t[thin] + 1)


def board(rows: list[dict], cfg: dict, n: int = 20, hide: bool = False) -> str:
    """Every open market (queued job x variant), one line each: unopposed first, then smallest matched stake, then newest.
    hide (an agent's view under [pit] blind): newest first, funding in place of pools, odds and the counter-bettor."""
    book, st = Book(rows), L.fold(rows)
    recs, lines, refs, typ = records(book), [], refuters(rows), typical_costs(rows, cfg) if hide else {}
    for jid, j in sorted(st.jobs.items(), key=lambda kv: kv[1]["added"], reverse=True):      # newest first; sort below is stable
        if j["state"] != "queued" or specmod.is_read(j["spec"]):
            continue
        s = j["spec"]
        prop = book.wallet(book.proposers.get(jid) or s.get("proposer") or HUMAN)
        for v in variants(s):
            t = book.totals(jid, v)
            side, x = pays(book, jid, v, "pass", cfg)
            name = jid if v == "main" else f"{jid}/{v}"
            name += specmod.change_mark(s)
            ctr = next((b for b in reversed(book.bets) if (b["job"], b["variant"]) == (jid, v) and b.get("why")
                        and b["side"] != "pass"), None)      # the latest counter-bettor's reason
            if hide:
                lines.append((0, f"{name} [{s['lane']}] {specmod.claim_first(s, 100)} · {funded(s, cfg)}{typical_here(s, cfg, typ)} · "
                                 f"proposer {prop} ({record(recs, prop)})"))
                continue
            lines.append(((min(t.values()) > 0, 2 * min(t.values())),
                          f"{name} [{s['lane']}, ${s.get('budget_usd', 0):g}] {specmod.claim_first(s, 100)} · PASS ${t['pass']:.2f} / FAIL ${t['fail']:.2f} · "
                          f"{side.upper()} pays {x:.1f}:1 · proposer {prop} ({record(recs, prop)})"
                          + (f' [{ctr["side"].upper()} {ctr["agent"]}: "{mark_refuted(ctr["why"][:80], refs)}"]' if ctr else "")))
    lines = [l for _, l in sorted(lines, key=lambda k: k[0])]
    return "\n".join(lines[:n] + ([f"… {len(lines) - n} more: q list --frontier"] if len(lines) > n else []))


def _cap(lines: list[str], n: int, what: str) -> list[str]:
    return lines if len(lines) <= n else lines[:n] + [f"  ... {len(lines) - n} more {what}"]


def thread(rows: list[dict], agent: str, hide: bool = False) -> str:
    """The context a spawned agent receives (<= 60 lines): brief, balance, its nodes in order with verdicts
    and findings, its open bets, and the open markets it has not bet on."""
    book, st = Book(rows), L.fold(rows)
    if agent not in book.agents:
        raise SystemExit(f"no agent {agent}")
    a, fam = book.agents[agent], book.family(agent)
    claimed = {r["job"] for r in rows if r["t"] == "claim" and r.get("agent") in fam}
    out = [f"{agent} ({record(records(book), book.wallet(agent))}; {a['kind']}{', under ' + a['parent'] if a.get('parent') else ''}) · balance "
           f"${book.balance(agent):.2f}" + (f" (wallet {book.wallet(agent)})" if book.wallet(agent) != agent else ""),
           f"brief: {a['brief']}" + (f" · runtime {a.get('runtime', 'claude')}{'/' + a['model'] if a.get('model') else ''}"
                                     if a.get("runtime") or a.get("model") else "")]
    z = sleepers(rows).get(agent)
    if z:
        out.append(f"awake: {z['wake']['reason']}" if z["wake"] else
                   f"sleeping until {until_text(z['sleep']['until'])} (since {z['sleep']['ts'][11:16]}Z): {z['sleep']['note']}")
    nodes = []
    for jid in sorted((j for j in st.jobs if book.proposers.get(j) in fam or j in claimed), key=lambda j: st.jobs[j]["added"]):
        j = st.jobs[jid]
        v = f" {j['result']['verdict']}" if j["result"] else ""
        nodes.append(f"  {jid} [{j['state']}{v}] ${j['spec']['budget_usd']} {specmod.claim_first(j['spec'], 80)}")
        if (j["result"] or {}).get("ref"):
            nodes.append(f"    ref: {j['spec'].get('ref_name', j['result']['ref'])} ({j['result']['ref']})")
            nodes.extend(f"    {line}" for line in j["result"].get("change", "").splitlines())
        if (j["result"] or {}).get("log"):
            nodes.append(f"    log: {j['result']['log']}")
        nodes += [f"    -> {fid}: {f['text'][:90]}" for fid, f in st.findings.items() if f["from"] == jid]
    out += ["nodes:"] + (_cap(nodes[::-1], 20, "earlier lines")[::-1] if nodes else ["  none yet"])
    mine = [b for b in book.bets if b["agent"] in fam and (b["job"], b["variant"]) not in book.settled]
    bets = [f"  {b['job']}/{b['variant']} {b['side'].upper()} ${b['usd']:.2f}" + ("" if hide else
            f"  (book PASS ${book.totals(b['job'], b['variant'])['pass']:.2f} / FAIL ${book.totals(b['job'], b['variant'])['fail']:.2f})") for b in mine]
    out += ["open bets:"] + (_cap(bets, 10, "bets") or ["  none"])
    have = {(b["job"], b["variant"]) for b in book.bets if b["agent"] in fam}
    markets = []
    for jid, j in st.jobs.items():
        if j["state"] != "queued" or specmod.is_read(j["spec"]):
            continue
        for v in variants(j["spec"]):
            if (jid, v) not in have:
                t = book.totals(jid, v)
                markets.append(f"  {jid}/{v}{specmod.change_mark(j['spec'])} " + ("" if hide else f"PASS ${t['pass']:.2f} / FAIL ${t['fail']:.2f} · ") + f"${j['spec']['budget_usd']} "
                               f"{j['spec']['lane']} · {specmod.claim_first(j['spec'], 60)}")
    out += ["open markets you have not bet on:"] + (_cap(markets, 20, "markets") or ["  none"])
    return "\n".join(out)


# ---- bootstrap: what a new agent pays to learn the market (agents/BOOTSTRAP.md is what it is told) -------------

def bootstrap_cost(rows: list[dict], agent: str, n: int = 20) -> dict:
    """Over the first n jobs the agent (or its subs) posted: the share that ended INVALID, and, secondarily, cancelled."""
    book, st = Book(rows), L.fold(rows)
    first = [j for j, p in book.proposers.items() if book.wallet(p) == agent and j in st.jobs][:n]
    k = len(first) or 1
    inv = sum(1 for j in first if (st.jobs[j]["result"] or {}).get("verdict") == "invalid")
    can = sum(1 for j in first if st.jobs[j]["state"] == "cancelled")
    return {"agent": agent, "posts": len(first), "invalid": round(inv / k, 4), "cancelled": round(can / k, 4)}


def newcomer(rows: list[dict], before: int | None = None) -> str | None:
    """The most recently registered persistent agent (among rows[:before] when given); reflect maintains the text, it is not a newcomer."""
    ids = [a for a, r in Book(rows[:before]).agents.items() if r["kind"] == "persistent" and a != REFLECT]
    return ids[-1] if ids else None


def newcomer_cost(rows: list[dict], n: int = 20) -> dict | None:
    a = newcomer(rows)
    return a and bootstrap_cost(rows, a, n)
