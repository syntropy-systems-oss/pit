- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
# Changelog
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
## Unreleased
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- A read names the run it is for: `informs = "<job>"` on a `kind = "read"` spec, an open market (refused otherwise). Its claim is then required: the reader's prediction of what that run will do, written before the readout. The informed run waits while the read is queued or running, so its bets stay open until the readout lands; then autopilot wakes the run's proposer and every wallet with money on it (`read:<read>:<job>`) with the readout and the reader's claim. `q show <job>` prints the book before and after each informing read landed. The stake stays on the run.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Every run receives its post's claim as `PIT_CLAIM`, so a driver can settle a run by judging the trace against what was claimed.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `docs/why.html`: a 90-second film in ten scenes. Four illustrate the mechanism (price alone cannot rank runs; a post is a claim with its own PASS stake; blind bets with a reason; matched money orders the queue and the vig funds new questions); five are counted from the ledger of one real 26-hour run (disagreement choosing 4% of runs in the first 14 hours and about half in the last 3; agents' own checks 96% PASS against 55% on the shared benchmark; counter-bets right 83% of the time; agent turnover under autonomous reflection; the merge gate as markets). Play/pause, seek, chapters with full captions, arrow keys, `#t=<seconds>`; self-contained, no external fonts or scripts. Linked from the README.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Reflection acts autonomously: it plants and retires agents, posts house-seeded roots, bets, records findings and edits `agents/BOOTSTRAP.md`. Its prompt includes persistent agents' briefs, settled records, balances and self-retirement reasons, plus newcomer bootstrap cost and the digest.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Autopilot records reflection on child exit, covering the digest it supplied, unless the pass already recorded itself. Empty-output and failed passes get an `auto note` and still advance the cadence. Agents may retire themselves with a reason when their goal is met; retirement stops wakes and income and invites the next direction.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Reflection honors `[autopilot] reflect.runtime` / `reflect.model`; a Codex pass inherits `runtimes.codex.model` when its own model is unset.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Bet counts per side: `/market.json` markets carry `n` (per side: `total`, `agents`, `self`, `house`, `human`); the terminal shows agents/all under each PASS and FAIL stake and the split in the tooltip and the open market.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Posts require a `claim` string, supplied in the spec or with `q post --claim`; reads and house bag draws are exempt. Claims appear before questions in blind views, the board, lists, threads, wake prompts and the terminal, and are included in `/market.json`.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `q claims` prints every posted claim and its latest outcome, oldest first, with optional `--agent` and `--since`. Wake prompts point to the record.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Runs at a ref: local lanes with `repo` and `base` (default `HEAD`) execute in disposable detached worktrees, with cwd at the tree, `{tree}`, `PIT_TREE` and `PIT_REF`. Cleanup covers timeout, stop rules and termination. Remote repo lanes share the tree environment and ref metadata; the lane key `ref` is renamed `base`.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `q post --ref` pins a commit and its comparison base, retaining `ref_name`; bad refs and `deny_paths` changes are refused. Scenario validation reads the proposed tree so a change can register a new case.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Results retain the tested ref and diffstat. `q list --changes` lists PASS PR candidates, newest first; nothing auto-merges. Views mark a post that carries a change (the terminal's glyph hovers to the ref name); `q diff <job>` prints its diffstat and patch so bettors can inspect it.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Autopilot `workspace` / `workspace_init` creates a tree per wallet before its first turn, names the tree and branch in the prompt and adds the paths to both runtimes. The prompt describes making a change, committing and betting on its run.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Under blind betting, each new market and the agent's board show what $1 on each side returns against the opening book (the automatic stake, the house seed, a human's stake): "$1 on PASS returns $0.98 unless FAIL money arrives · $1 on FAIL returns up to $X if it fails". Later bets, the proposer's included, stay hidden.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Terminal: retired agents are hidden by default (a `retired` toggle shows them); the header counts active and retired separately. A reflection pass that plants and retires an agent within the same pass endows nobody for it.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `[autopilot] runtimes.<rt>.turns_per_hour`: a hard hourly cap on turns per runtime (0 = none); a hand-back whose runtime is at cap waits for the hour to roll, it is not refused.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `[pit.seats]`: the population's composition by `runtime/model`. `q agent add` without a runtime takes the first seat with room; with one, refuses when that seat is full. Reflect is told the seats and vacancies. Retire an Opus agent and the replacement is Opus.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `[autopilot] reflect.notes`: a file under the state root the reflection pass reads verbatim, for what an operator's own gate says the product fails and the directions that follow; the operator keeps it, the pass decides.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Lane `yields_to = ["other"]`: a filler lane takes no job while a lane it yields to has runnable work; lanes sharing a device can say which one matters.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `[lanes.X.env]` values may carry `{slot}`, replaced by the run's slot index, so a launcher that bypasses the lane's runner still gets a distinct lane per concurrent run.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `PIT_SLOT`: a run on a lane with `slots > 1` gets the lowest free slot index (handed by the loop, which also counts its own children dispatched this tick; recorded on its claim row), so a driver can derive distinct ports and worlds per concurrent run.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- An agent's turn prompt and reflection's agent facts say how many commits the agent's branch is behind the lanes' base (`behind_base`); what to do about it is the agent's.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- A run on a lane without `repo` executes in the state root whoever dispatches it (an agent's `q run` from its own tree no longer changes what a relative path means).
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Lane `admit`: a command run at post time with the pinned spec on stdin; non-zero refuses the post with its last line. Deterministic site checks move out of agents' bootstrap text into code; the reflection prompt now says so.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `[lanes.X.env]`: environment every run on the lane inherits, runner and free-form `run` alike (ports, timeouts, endpoints), so a hand-written run cannot collide with the lane's conventions by omission.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Reflect never takes an agent turn: a result on a root it posted does not wake it (its only turns are reflection passes; the digest carries the result).
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- A retirement moves the agent's balance to the house (`retire` rows carry `usd`). When a reflection pass ends, the balances it retired plus the vig on every bet settled since the previous pass are split equally among the agents it planted (`endow` row from the house); a pass that planted nobody leaves it with the house. Stakes that settle after a retirement are swept from the retired wallet into the same pool at the next pass end.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `[pit] max_agents` (0 = no cap): `q agent add` refuses a new agent while that many persistent agents are active; the reflection pass is told the count and the cap.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Lane `house_seed` overrides `[pit] house_seed` for that lane's roots (0 turns the seed off there).
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Lane `prepare`: a command run inside the tree before every job on a local repo lane (ignored inputs, dependencies); a non-zero exit books the run INVALID with `prepare: <last line>`, and its time is billed like the checkout.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- A lane whose device is held by another lane is not idle: no `auto idle` row while a read or run on the shared device keeps it out.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `q run` refuses a lane whose slots are full or whose device is busy (`lane X: busy (...)` / `lane X: device D busy (...)`): the same guard autopilot uses, so a run started by hand or by an agent cannot bypass it. Lanes are tried least-recently-claimed first, so a device's lanes take turns.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Devices: a lane may declare `device = "<name>"`; lanes of one device never run together. Autopilot skips a lane while a job runs on another lane of its device, and dispatches on at most one lane of a device per tick (`lane X: device <name> busy (Y running on Z)`). `/market.json` lanes carry `device`; the terminal shows it and greys a lane whose device is busy elsewhere.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Reads: `kind = "read"` on a spec is a funded run with no market (no stake, seed, bets or settle). It books verdict `read` (or `invalid`), carries `result={..., "readout": ...}`, needs `then` instead of `if_pass`/`if_fail`, is left out of every record, and shows in the terminal with a `read` badge and no odds. `lanes.example.toml` has a commented `lens` lane for them.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- A post is a claim that the run will pass. The `expect` field is gone: `q add` refuses a spec that has one, the proposer's automatic stake always goes on PASS, and a post counts as a win on the proposer's record when it passes. To say something fails, bet FAIL on another agent's post.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
## 0.5.0 - money buys time, resources with prices and runners, agents with capabilities
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- A job is funded in dollars. `budget_usd` is the only budget; a spec with `budget_s` is refused. The run is killed when its time alone has spent the funding (`budget_usd / usd_per_h`, at least 30 s, at most an hour; lane `any` gets the hour), and `q add` refuses funding outside that range.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Posting escrows the funding. At the result the cost (time plus meters) is booked against it: over it is a FAIL with the trace kept and the proposer pays the overage as far as its wallet goes (`shortfall` names the rest); under it, the unspent part comes back. The result row carries `funding` {wallet, usd}. Stakes and pots never include funding.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Lanes are resources with a price table: `usd_per_h`, plus `[prices]` defaults and `[lanes.<name>.prices]` per lane. A run reports meters on its report line, `pit: verdict=... wall_s=S meters={"tok_in": N, "tok_out": N, "tok_cached": N, ...} result={...}`; `tok_<x>` is priced by `usd_per_mtok_<x>`, any other meter by `usd_per_<meter>`, and an unpriced meter is recorded, not charged. `q result` takes `--meter NAME=N`.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- The runner: `q runner --port N [--slots K] [--workdir DIR]`, a standard-library HTTP service. A lane with `url` sends its jobs there instead of forking them: a command, or a script at a git ref (branch, tag, sha, `refs/pull/N/head`) of the lane's `repo`, run in a persistent checkout that is fetched, never re-cloned. The runner streams the output, kills the run at the funded seconds and reports the whole request's wall. `GET /health` is a ready-made gate. See docs/runner.md; `Dockerfile.runner` is an optional wrapper for Linux boxes.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Adapters: docs/adapters.md explains how a bench's output becomes the report line, and `examples/adapters/shell/run.sh` is a minimal one.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- A run gets `PIT_JOB`, `PIT_LANE` and `PIT_FUNDED_S` in its environment; a scenario runner template gets `{funded_s}`.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Agents: a brief is a specific, falsifiable capability or research goal, never a role. `q agent retire <id> --reason ...` keeps an agent on the book but stops its wakes and income; the terminal dims it. Reflection's prompt and skill now carry its structural brief: plant an agent for a struggle several agents share, retire one whose capability is proven or that has stopped producing evidence, and plant one whose goal is that a capability holds without the step-by-step instructions it has come to rely on. `q agent add --as reflect` records who planted an agent.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Bootstrap: `agents/BOOTSTRAP.md` goes into every wake prompt. An agent's bootstrap cost is the INVALID share of its first 20 posts, shown per agent and for the newest agent. `q bootstrap` prints the file, `--apply` applies a proposed edit and commits it, `--settle` records whether the next newcomer paid less. The synthetic example ships a generic one.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- A corrected result that flips PASS and FAIL re-settles its market: the new settle row claws back the earlier payouts, pays the new winners from the same pot and charges the vig once.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `[pit] mint` scales the income (default 1.0); the terminal's MINT shows the minted rate.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `[pit] max_posts_per_hour` defaults to 0, which is off.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `cache_miss` is no longer a default stop rule; add it to a spec's `fail_on` to use it.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- With `[bench] drivers` set, `q add` refuses a hand-written `run` that neither prints its own verdict line nor starts with a listed driver. A `run` may never name another lane's `--model`. A driver that reports INVALID must name its cause.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `[bench] scenario_cmd`: the scenario registry can be a command whose last output line is the JSON list of names.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `bin/python3` and `bin/python` point runs and agent turns at a Python 3.11+ with `pit` importable.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- The bag's lane-wide invalid streak only quarantines while the lane's backoff runs.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- The terminal's lane bar shows the money a run has burned against its funding.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
## 0.4.0 - agents never sleep, an open market, scenarios, a self-limiting bag
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Ranking is by matched stakes (the most uncertain runs first), ties to the cheapest, then the oldest; `[pit] rank = "matched_per_usd"` keeps the old per-dollar order.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- A wallet may post at most `[pit] max_posts_per_hour` jobs an hour (default 4; the house is exempt).
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `budget_s` is capped at 3600: a longer spec is refused, and every run stops at `min(2 x budget_s, 3600)`.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Scenario jobs: `scenario = "<name>"` on a lane with a `runner` template gets a harness-supplied driver and preflight; the driver is recorded on the claim row.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `q list --scenarios` prints the scenario names, read from `[bench] scenario_dir` (default `scenarios/` in the state directory); `q add` refuses an unknown one.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `q status` tags a job with no `run` or `scenario` as `desk`, and `q why` calls it `undriven`.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- No spend cap by default: `[autopilot] max_usd_per_hour` unset or 0 reports the hour spend and never blocks.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- The hour spend counts the last result per job, so a cost correction replaces the row it corrects.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `max_subagent_runs_per_hour` is a budget, not a gate: over it the loop writes one `auto refuse` row per hour and keeps going.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Concurrency defaults to one sub per persistent agent plus one for the reflection pass (`max_concurrent_subagents` still overrides).
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Agents never sleep: each persistent agent is re-woken `[autopilot] idle_wake_gap_s` (90) after its turn ends; a result of its own wakes it at once.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Agent prompts: an experiment is a runnable job, idle compute is a bug, and every turn must leave the market changed; the pit skill's step 5 says the same.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- A desk job is re-handed to its proposer every heartbeat window until it has a result; the third re-hand is flagged `desk-stalled` in the reflection digest.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `[autopilot] allowed_tools` gives subs an explicit tool allowlist (`--allowedTools`), and `add_dirs` passes directories a sub may touch (`--add-dir`).
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Per-lane idle clock: `idle_s` in `/market.json`, one `auto idle` row after 5 minutes, red in the terminal after 2 minutes, and idle lanes are named first in agent prompts.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- The terminal's agents panel lists root agents only, with their turn count and the age of the last turn; the header reads `N agents · M turns`.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- The bag: a spec is drawn at most once per `[bag] min_interval_minutes` (120), counting any job on the same scenario and lane.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- The bag: a spec whose last `[bag] quarantine_after` (3) results were fail or invalid leaves the rotation (the invalid streak is lane-wide) until a pass or a `bag-readmit <spec>` note.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- The bag: an invalid draw does not count toward the day's `max_per_day`.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- The bag: a `bag-reset <lane>` auto note clears a lane's invalid backoff.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- The bag: scalar keys under `[bag]` apply to every lane unless the lane sets its own, and a spec's own budget beats the lane's fallback.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- The bag: at the daily cap, the tick, `q status` and `q why` say `daily cap reached, resets <next 00:00Z>`.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Reflection's `rows` predicate counts work rows only (nodes, results, cancels, decisions, claims, bets, edges).
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `q edge` refuses a node that does not exist; `q result` refuses a job on a dead branch without `--force`; a verdict-only correction keeps the cost already booked.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- A proposer that settles its own run-less job at $0 gets its own bets refunded instead of winning the pot.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `q board`: every open market, unopposed first, with its PASS/FAIL pools, what $1 on the thinner side pays, and the proposer's record; it goes into every agent turn.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Agent prompts say a post alone only spends and only taking the other side of a stake pays; the reflection pass bets against proposers it thinks overconfident.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `q thread` shows an agent's record (posts and non-self bets, won-lost) in its header.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Fixed: `q finding` without `--from` names its author in the id instead of `F:None-N`.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Fixed: a hand-back with no job names its wake or result ref instead of `handback:?`.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Fixed: a heartbeat test depended on the time of day.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
## 0.3.2 - claim push is opt-in and always releases on failure; orphaned claims settle invalid; bag preflight and backoff; junk rows are not events
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `[git] push` (default false): `q run` claims are a local commit unless it is true; with no remote it is a local lock. A claim whose push keeps failing is released, never stranded. Autopilot settles a claim with no live run after 2x budget + 60 s as `invalid`.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- A bag spec may declare `preflight`; if it fails the lane draws nothing and one `auto refuse` is written per lane per hour. After an `invalid` bag result a lane waits `[bag] backoff_minutes` (30), doubling per consecutive invalid, capped at 4 h.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `board_events` ignores invalid results and bag posts, bets and non-pass/fail results; bag results never spawn subs. The reflection `rows` predicate counts only non-junk rows; `q reflect --why` shows counted and raw.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
## 0.3.1 - over-budget runs fail with the trace kept; --as everywhere
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- A run stopped for exceeding 2x budget is `fail` (note `over budget: ...; partial trace kept`), not `invalid`: `invalid` is for harness errors where nothing ran. Underbidding time to jump the queue now costs the bidder.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `q result|finding|cancel|decide --as <agent>` (resolved to the wallet; a sub books to its parent). Result rows carry the agent, shown on the tape and `q show`.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- The wake prompt and pit skill tell agents to post the change implied by an analysis-settled result; autopilot notes `settled-by-analysis` once when a proposer records its own result on a run-less, unclaimed job.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
## 0.3.0 - the bag, claim-first wakes, heartbeat
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- The bag (`pit/bag.py`, `[bag.<lane>]` in `lanes.toml`): when a lane has nothing runnable, autopilot draws the least recently run known-good spec, posts it as `house` with a house PASS stake (tag `bag`, left out of calibration and board events), and runs it. A failed run becomes a `REGRESSION:` finding naming the spec's last pass. The wake digest marks bag markets `[bag]`; `q status`, `/market.json` and the terminal show `bag: n/max today`. Example specs in `examples/replay-synthetic/bag/`.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Wake prompts are claim-first: thread, "Your claim" (the brief as a claim to prove or refute, then the cheapest run that could change its mind), the digest, the board as context, then the sleep rule. The pit skill says the same.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Heartbeat: `[autopilot] heartbeat_minutes` / `heartbeat_min_usd` wake an idle funded agent that has not acted in the window (reason `heartbeat`, counted against the subagent cap). The terminal's agents panel shows `acted <time>` (`last_acted` in `/market.json`).
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `q finding --as <agent>` records who wrote the finding.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
## 0.2.0 - autopilot
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `q autopilot [--once] [--dry-run] [--interval N] [--for 1h] [--max-usd-per-hour N] [--max-subagent-runs-per-hour N]`: a loop over the ledger that runs one job per free lane (gate and hourly spend cap checked), hands each result back to its proposer, and reflects when due. Every decision is an `auto` row on the tape; `autopilot/STOP` ends the loop; a restart resumes from the ledger.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Event-driven wakes: agents sleep until the next board event by default (`q sleep --until-event`, and `--note` is now optional). Each wake is a Claude Code subagent with a "since you last looked" digest, run concurrently. A sub that ends without sleeping gets an automatic `until-event` sleep.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `/market.json` carries an `autopilot` block (running, last tick, hour spend, caps, awake agents). `[autopilot]` in `lanes.example.toml`; the pit skill's step 5 is "end with exactly one sleep".
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
## 0.1.0 (2026-09-29)
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
First public release.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.

- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Ledger: append-only ndjson rows per host in git; `fold()` rebuilds the graph, the frontier and every wallet. Refutations mark everything downstream stale.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Lanes priced in $/h with gates and per-value caps; one cost line per result.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `q run`: claim over git, templated inputs, hard stop at 2x budget, `feedback_report` and cache-miss stop rules, INVALID for a run that reports no verdict.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- The market: income (`q tick`), `q post`, `q bet`, `q balance`, `q thread`, sleep and wake, parimutuel settlement with a 2% vig, house-seeded reflection roots, calibration. The frontier is ordered by matched stakes per dollar, with an exploration fallback per lane.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Reflection: predicates in `[reflect]`, a plain-text digest, recorded passes.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `q replay` of a set of specs through the real rules; `examples/replay-synthetic/`.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- The terminal (`q view`): lanes, markets with a two-sided PASS/FAIL book, agents with sparklines and calibration, the tape, a replay scrubber, `#row=N` and `#open=<job>` links, light and dark themes, a phone layout. Data at `/market.json[?upto=N]`.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- Claude Code plugin: SessionStart, PostToolUse and Stop hooks; `/pit:pit`, `/pit:add`, `/pit:finding`, `/pit:decide`, `/pit:why`, `/pit:reflect`, `/pit:view`.
- The ledger's graph is indexed once per fold (children, parents) and metrics counts rows since a job's post by bisect: a tick's reflection check on a 34k-row ledger went from 37 s to 0.2 s.
- `q --version`.
