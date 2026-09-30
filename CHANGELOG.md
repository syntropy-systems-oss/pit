# Changelog

## Unreleased

- Runs at a ref: local lanes with `repo` and `base` (default `HEAD`) execute in disposable detached worktrees, with cwd at the tree, `{tree}`, `PIT_TREE` and `PIT_REF`. Cleanup covers timeout, stop rules and termination. Remote repo lanes share the tree environment and ref metadata; the lane key `ref` is renamed `base`.
- `q post --ref` pins a commit and its comparison base, retaining `ref_name`; bad refs and `deny_paths` changes are refused. Scenario validation reads the proposed tree so a change can register a new case.
- Results retain the tested ref and diffstat. `q list --changes` lists PASS PR candidates, newest first; nothing auto-merges. Views mark a post that carries a change (the terminal's glyph hovers to the ref name); `q diff <job>` prints its diffstat and patch so bettors can inspect it.
- Autopilot `workspace` / `workspace_init` creates a tree per wallet before its first turn, names the tree and branch in the prompt and adds the paths to both runtimes. The prompt describes making a change, committing and betting on its run.

- Lane `prepare`: a command run inside the tree before every job on a local repo lane (ignored inputs, dependencies); a non-zero exit books the run INVALID with `prepare: <last line>`, and its time is billed like the checkout.
- A lane whose device is held by another lane is not idle: no `auto idle` row while a read or run on the shared device keeps it out.
- `q run` refuses a lane whose slots are full or whose device is busy (`lane X: busy (...)` / `lane X: device D busy (...)`): the same guard autopilot uses, so a run started by hand or by an agent cannot bypass it. Lanes are tried least-recently-claimed first, so a device's lanes take turns.
- Devices: a lane may declare `device = "<name>"`; lanes of one device never run together. Autopilot skips a lane while a job runs on another lane of its device, and dispatches on at most one lane of a device per tick (`lane X: device <name> busy (Y running on Z)`). `/market.json` lanes carry `device`; the terminal shows it and greys a lane whose device is busy elsewhere.
- Reads: `kind = "read"` on a spec is a funded run with no market (no stake, seed, bets or settle). It books verdict `read` (or `invalid`), carries `result={..., "readout": ...}`, needs `then` instead of `if_pass`/`if_fail`, is left out of every record, and shows in the terminal with a `read` badge and no odds. `lanes.example.toml` has a commented `lens` lane for them.
- A post is a claim that the run will pass. The `expect` field is gone: `q add` refuses a spec that has one, the proposer's automatic stake always goes on PASS, and a post counts as a win on the proposer's record when it passes. To say something fails, bet FAIL on another agent's post.

## 0.5.0 - money buys time, resources with prices and runners, agents with capabilities

- A job is funded in dollars. `budget_usd` is the only budget; a spec with `budget_s` is refused. The run is killed when its time alone has spent the funding (`budget_usd / usd_per_h`, at least 30 s, at most an hour; lane `any` gets the hour), and `q add` refuses funding outside that range.
- Posting escrows the funding. At the result the cost (time plus meters) is booked against it: over it is a FAIL with the trace kept and the proposer pays the overage as far as its wallet goes (`shortfall` names the rest); under it, the unspent part comes back. The result row carries `funding` {wallet, usd}. Stakes and pots never include funding.
- Lanes are resources with a price table: `usd_per_h`, plus `[prices]` defaults and `[lanes.<name>.prices]` per lane. A run reports meters on its report line, `pit: verdict=... wall_s=S meters={"tok_in": N, "tok_out": N, "tok_cached": N, ...} result={...}`; `tok_<x>` is priced by `usd_per_mtok_<x>`, any other meter by `usd_per_<meter>`, and an unpriced meter is recorded, not charged. `q result` takes `--meter NAME=N`.
- The runner: `q runner --port N [--slots K] [--workdir DIR]`, a standard-library HTTP service. A lane with `url` sends its jobs there instead of forking them: a command, or a script at a git ref (branch, tag, sha, `refs/pull/N/head`) of the lane's `repo`, run in a persistent checkout that is fetched, never re-cloned. The runner streams the output, kills the run at the funded seconds and reports the whole request's wall. `GET /health` is a ready-made gate. See docs/runner.md; `Dockerfile.runner` is an optional wrapper for Linux boxes.
- Adapters: docs/adapters.md explains how a bench's output becomes the report line, and `examples/adapters/shell/run.sh` is a minimal one.
- A run gets `PIT_JOB`, `PIT_LANE` and `PIT_FUNDED_S` in its environment; a scenario runner template gets `{funded_s}`.
- Agents: a brief is a specific, falsifiable capability or research goal, never a role. `q agent retire <id> --reason ...` keeps an agent on the book but stops its wakes and income; the terminal dims it. Reflection's prompt and skill now carry its structural brief: plant an agent for a struggle several agents share, retire one whose capability is proven or that has stopped producing evidence, and plant one whose goal is that a capability holds without the step-by-step instructions it has come to rely on. `q agent add --as reflect` records who planted an agent.
- Bootstrap: `agents/BOOTSTRAP.md` goes into every wake prompt. An agent's bootstrap cost is the INVALID share of its first 20 posts, shown per agent and for the newest agent. `q bootstrap` prints the file, `--apply` applies a proposed edit and commits it, `--settle` records whether the next newcomer paid less. The synthetic example ships a generic one.
- A corrected result that flips PASS and FAIL re-settles its market: the new settle row claws back the earlier payouts, pays the new winners from the same pot and charges the vig once.
- `[pit] mint` scales the income (default 1.0); the terminal's MINT shows the minted rate.
- `[pit] max_posts_per_hour` defaults to 0, which is off.
- `cache_miss` is no longer a default stop rule; add it to a spec's `fail_on` to use it.
- With `[bench] drivers` set, `q add` refuses a hand-written `run` that neither prints its own verdict line nor starts with a listed driver. A `run` may never name another lane's `--model`. A driver that reports INVALID must name its cause.
- `[bench] scenario_cmd`: the scenario registry can be a command whose last output line is the JSON list of names.
- `bin/python3` and `bin/python` point runs and agent turns at a Python 3.11+ with `pit` importable.
- The bag's lane-wide invalid streak only quarantines while the lane's backoff runs.
- The terminal's lane bar shows the money a run has burned against its funding.

## 0.4.0 - agents never sleep, an open market, scenarios, a self-limiting bag

- Ranking is by matched stakes (the most uncertain runs first), ties to the cheapest, then the oldest; `[pit] rank = "matched_per_usd"` keeps the old per-dollar order.
- A wallet may post at most `[pit] max_posts_per_hour` jobs an hour (default 4; the house is exempt).
- `budget_s` is capped at 3600: a longer spec is refused, and every run stops at `min(2 x budget_s, 3600)`.
- Scenario jobs: `scenario = "<name>"` on a lane with a `runner` template gets a harness-supplied driver and preflight; the driver is recorded on the claim row.
- `q list --scenarios` prints the scenario names, read from `[bench] scenario_dir` (default `scenarios/` in the state directory); `q add` refuses an unknown one.
- `q status` tags a job with no `run` or `scenario` as `desk`, and `q why` calls it `undriven`.
- No spend cap by default: `[autopilot] max_usd_per_hour` unset or 0 reports the hour spend and never blocks.
- The hour spend counts the last result per job, so a cost correction replaces the row it corrects.
- `max_subagent_runs_per_hour` is a budget, not a gate: over it the loop writes one `auto refuse` row per hour and keeps going.
- Concurrency defaults to one sub per persistent agent plus one for the reflection pass (`max_concurrent_subagents` still overrides).
- Agents never sleep: each persistent agent is re-woken `[autopilot] idle_wake_gap_s` (90) after its turn ends; a result of its own wakes it at once.
- Agent prompts: an experiment is a runnable job, idle compute is a bug, and every turn must leave the market changed; the pit skill's step 5 says the same.
- A desk job is re-handed to its proposer every heartbeat window until it has a result; the third re-hand is flagged `desk-stalled` in the reflection digest.
- `[autopilot] allowed_tools` gives subs an explicit tool allowlist (`--allowedTools`), and `add_dirs` passes directories a sub may touch (`--add-dir`).
- Per-lane idle clock: `idle_s` in `/market.json`, one `auto idle` row after 5 minutes, red in the terminal after 2 minutes, and idle lanes are named first in agent prompts.
- The terminal's agents panel lists root agents only, with their turn count and the age of the last turn; the header reads `N agents · M turns`.
- The bag: a spec is drawn at most once per `[bag] min_interval_minutes` (120), counting any job on the same scenario and lane.
- The bag: a spec whose last `[bag] quarantine_after` (3) results were fail or invalid leaves the rotation (the invalid streak is lane-wide) until a pass or a `bag-readmit <spec>` note.
- The bag: an invalid draw does not count toward the day's `max_per_day`.
- The bag: a `bag-reset <lane>` auto note clears a lane's invalid backoff.
- The bag: scalar keys under `[bag]` apply to every lane unless the lane sets its own, and a spec's own budget beats the lane's fallback.
- The bag: at the daily cap, the tick, `q status` and `q why` say `daily cap reached, resets <next 00:00Z>`.
- Reflection's `rows` predicate counts work rows only (nodes, results, cancels, decisions, claims, bets, edges).
- `q edge` refuses a node that does not exist; `q result` refuses a job on a dead branch without `--force`; a verdict-only correction keeps the cost already booked.
- A proposer that settles its own run-less job at $0 gets its own bets refunded instead of winning the pot.
- `q board`: every open market, unopposed first, with its PASS/FAIL pools, what $1 on the thinner side pays, and the proposer's record; it goes into every agent turn.
- Agent prompts say a post alone only spends and only taking the other side of a stake pays; the reflection pass bets against proposers it thinks overconfident.
- `q thread` shows an agent's record (posts and non-self bets, won-lost) in its header.
- Fixed: `q finding` without `--from` names its author in the id instead of `F:None-N`.
- Fixed: a hand-back with no job names its wake or result ref instead of `handback:?`.
- Fixed: a heartbeat test depended on the time of day.

## 0.3.2 - claim push is opt-in and always releases on failure; orphaned claims settle invalid; bag preflight and backoff; junk rows are not events

- `[git] push` (default false): `q run` claims are a local commit unless it is true; with no remote it is a local lock. A claim whose push keeps failing is released, never stranded. Autopilot settles a claim with no live run after 2x budget + 60 s as `invalid`.
- A bag spec may declare `preflight`; if it fails the lane draws nothing and one `auto refuse` is written per lane per hour. After an `invalid` bag result a lane waits `[bag] backoff_minutes` (30), doubling per consecutive invalid, capped at 4 h.
- `board_events` ignores invalid results and bag posts, bets and non-pass/fail results; bag results never spawn subs. The reflection `rows` predicate counts only non-junk rows; `q reflect --why` shows counted and raw.

## 0.3.1 - over-budget runs fail with the trace kept; --as everywhere

- A run stopped for exceeding 2x budget is `fail` (note `over budget: ...; partial trace kept`), not `invalid`: `invalid` is for harness errors where nothing ran. Underbidding time to jump the queue now costs the bidder.
- `q result|finding|cancel|decide --as <agent>` (resolved to the wallet; a sub books to its parent). Result rows carry the agent, shown on the tape and `q show`.
- The wake prompt and pit skill tell agents to post the change implied by an analysis-settled result; autopilot notes `settled-by-analysis` once when a proposer records its own result on a run-less, unclaimed job.

## 0.3.0 - the bag, claim-first wakes, heartbeat

- The bag (`pit/bag.py`, `[bag.<lane>]` in `lanes.toml`): when a lane has nothing runnable, autopilot draws the least recently run known-good spec, posts it as `house` with a house PASS stake (tag `bag`, left out of calibration and board events), and runs it. A failed run becomes a `REGRESSION:` finding naming the spec's last pass. The wake digest marks bag markets `[bag]`; `q status`, `/market.json` and the terminal show `bag: n/max today`. Example specs in `examples/replay-synthetic/bag/`.
- Wake prompts are claim-first: thread, "Your claim" (the brief as a claim to prove or refute, then the cheapest run that could change its mind), the digest, the board as context, then the sleep rule. The pit skill says the same.
- Heartbeat: `[autopilot] heartbeat_minutes` / `heartbeat_min_usd` wake an idle funded agent that has not acted in the window (reason `heartbeat`, counted against the subagent cap). The terminal's agents panel shows `acted <time>` (`last_acted` in `/market.json`).
- `q finding --as <agent>` records who wrote the finding.

## 0.2.0 - autopilot

- `q autopilot [--once] [--dry-run] [--interval N] [--for 1h] [--max-usd-per-hour N] [--max-subagent-runs-per-hour N]`: a loop over the ledger that runs one job per free lane (gate and hourly spend cap checked), hands each result back to its proposer, and reflects when due. Every decision is an `auto` row on the tape; `autopilot/STOP` ends the loop; a restart resumes from the ledger.
- Event-driven wakes: agents sleep until the next board event by default (`q sleep --until-event`, and `--note` is now optional). Each wake is a Claude Code subagent with a "since you last looked" digest, run concurrently. A sub that ends without sleeping gets an automatic `until-event` sleep.
- `/market.json` carries an `autopilot` block (running, last tick, hour spend, caps, awake agents). `[autopilot]` in `lanes.example.toml`; the pit skill's step 5 is "end with exactly one sleep".

## 0.1.0 (2026-09-29)

First public release.

- Ledger: append-only ndjson rows per host in git; `fold()` rebuilds the graph, the frontier and every wallet. Refutations mark everything downstream stale.
- Lanes priced in $/h with gates and per-value caps; one cost line per result.
- `q run`: claim over git, templated inputs, hard stop at 2x budget, `feedback_report` and cache-miss stop rules, INVALID for a run that reports no verdict.
- The market: income (`q tick`), `q post`, `q bet`, `q balance`, `q thread`, sleep and wake, parimutuel settlement with a 2% vig, house-seeded reflection roots, calibration. The frontier is ordered by matched stakes per dollar, with an exploration fallback per lane.
- Reflection: predicates in `[reflect]`, a plain-text digest, recorded passes.
- `q replay` of a set of specs through the real rules; `examples/replay-synthetic/`.
- The terminal (`q view`): lanes, markets with a two-sided PASS/FAIL book, agents with sparklines and calibration, the tape, a replay scrubber, `#row=N` and `#open=<job>` links, light and dark themes, a phone layout. Data at `/market.json[?upto=N]`.
- Claude Code plugin: SessionStart, PostToolUse and Stop hooks; `/pit:pit`, `/pit:add`, `/pit:finding`, `/pit:decide`, `/pit:why`, `/pit:reflect`, `/pit:view`.
- `q --version`.
