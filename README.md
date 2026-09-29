# Pit

[![CI](https://github.com/syntropy-systems-oss/pit/actions/workflows/ci.yml/badge.svg)](https://github.com/syntropy-systems-oss/pit/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

Pit is a prediction market that schedules experiments. You write each experiment down as a question with a prediction and a price. Agents earn a steady income, pay for the runs they post, and bet PASS or FAIL on each run; the runs they disagree about go first, because those are the ones whose result is not already known. Everything (jobs, results, costs, findings, bets) is a row in an append-only ledger kept in git, and a finding that later turns out wrong marks everything that rested on it stale. Pit records and runs; a Claude Code session, or you, decides. Python 3.11+, standard library only, no build step.

Pit grew out of Trellis, the ledger-and-lanes frame underneath it.

![The Pit terminal replaying the synthetic example at ledger row 29](docs/terminal.png)

## Quickstart (60 seconds)

```sh
git clone https://github.com/syntropy-systems-oss/pit && cd pit
bin/q --version                                   # pit 0.1.0
bin/q replay examples/replay-synthetic            # re-run a synthetic night through the real rules
PIT_ROOT=examples/replay-synthetic bin/q status   # the frontier, spend and stale list
PIT_ROOT=examples/replay-synthetic bin/q view     # the terminal at http://127.0.0.1:8790/
```

Open `http://127.0.0.1:8790/#row=29&open=variant-b` to see the night half-way through, with one market open. Press `?` for the keys.

As a Claude Code plugin (the path of your clone, or the GitHub `owner/repo`):

```
/plugin marketplace add ./pit
/plugin install pit@pit
```

Then give Pit a state directory, its own git repository:

```sh
mkdir ~/pit-state && cd ~/pit-state && git init
cp <clone>/lanes.example.toml lanes.toml     # name your lanes, set their $/h and gates
mkdir ledger queue
export PIT_ROOT=~/pit-state                  # e.g. in your shell profile
```

`pip install -e .` (or `uv tool install .`) puts `q` on your PATH; `bin/q` works without installing.

`examples/replay-synthetic/` is a made-up evening of eleven jobs: a baseline, a variant B that seems to beat it, a leak check that refutes that finding, an ablation the refutation makes stale, a large-model sweep cut short by a stop rule, a CI chain, two agents (`explorer`, `skeptic`) betting against each other, and one reflection. `q replay` prints the order a human ran things in next to the order Pit's rules pick, the wall time and dollars per lane, and where the rules saved (in this example, $26 and 77 minutes of critical path).

## Concepts

### Ledger and graph

Every event is a row in `ledger/<host>.ndjson`, appended and committed with `git -C $PIT_ROOT`: `node` (job, finding, hypothesis), `edge` (depends-on, produces, edits, refutes, refines, supersedes), `claim`, `release`, `result`, `decision`, `cancel`, `review`, `reflect`, and the market's `agent`, `drip`, `bet`, `settle`, `sleep`, `wake`. Nothing else is stored. `fold()` rebuilds the graph, the frontier and every wallet from the rows.

A `refutes` edge marks everything downstream of the refuted node **stale** until someone reviews it (`q review <id> --note ...`) or cancels it; stale jobs leave the frontier. Several hosts can share one ledger through a common git remote: each host appends only to its own file, so merges never conflict, and a claim is won by whichever push lands first.

### Lanes and prices

A lane is somewhere work runs: a GPU box, CI runners, or `any` (wherever the session is; tokens only). `lanes.toml` gives each lane a `usd_per_h`, `slots`, a `gate` (a shell command that exits 0 when the lane is free, for example only while a production box is idle) and an optional `max_usd_per_value`, so a cheap lane refuses an expensive question. Prices are attention, not rental: what an hour of that box is worth to you. Every result carries a cost line (tokens plus wall time at the lane's rate), so "dollars per changed decision" is a sum over the ledger. See [`lanes.example.toml`](lanes.example.toml).

### Jobs and rungs

One TOML file per job; `q add` validates it and appends it.

```toml
id = "variant-b"
question = "Does variant B beat A on the held-out set?"
expect = "pass"                        # your prediction: pass | fail
if_pass = "promote B; queue the ablation"
if_fail = "keep A as the default"      # must differ from if_pass
lane = "gpu-small"
budget_s = 1800                        # hard stop at 2x
budget_usd = 8
value = 1                              # questions it settles
depends_on = ["baseline-a@pass"]       # jobs or findings; "@pass" = only on that branch
inputs = { baseline = "{{ jobs.baseline-a.result.acc }}" }
run = "python3 eval.py --variant B --beat {{ inputs.baseline }}"
produces_if_pass = ["F:b-beats-a"]
```

Experiments climb in rungs. Each job names the branch it needs from the one below (`depends_on = ["job@pass"]`), so the next rung runs only if the last one came out that way, and a rung that ends on the other branch closes the ladder above it (`q why <id>` says "dead branch: cancel it"). `q add` refuses a spec with no prediction, identical branches, an unknown lane, a budget over the lane's cap, or a template that reads something outside `depends_on`.

`q run <id>` checks the gate, claims the job, fills templates from upstream results (shell-quoted), runs the command with a hard stop at 2x `budget_s`, and appends the result. The command reports with one line of output:

```
pit: job=<id> verdict=pass uncached=1200 cached=48000 out=900 wall_s=74.7 result={"acc": 0.78}
```

A `feedback_report` or cache-miss line fails the run with that line as the reason; 2x `budget_s`, or exiting without a verdict, makes it INVALID. A non-run is not evidence.

### Reflection

When a predicate in `[reflect]` fires (`rows` since the last reflection, `hours` since it, `after` a row type such as `result:invalid`, `spend_rising` over the last N results), `q status` says `reflect due`. `q reflect --since-last` prints a plain-text digest; the `/pit:reflect` skill hands it to one stronger model, which proposes new job specs, bets on open jobs where it has a reason, and suggests hypotheses. The session keeps what it agrees with and records the pass with `q reflect --record --as reflect`.

### The Pit: three verbs, four sentences

An agent is told four sentences:

> You receive a steady income. You can post runs (you pay for them) and bet on whether each variant will pass. Runs that agents disagree about get scheduled first. If you're right, you win the pot and can afford more runs.

and has three verbs:

| verb | command | what happens |
|---|---|---|
| **post** | `q post <spec.toml> --as <agent>` | the wallet pays `budget_usd`; the agent's `expect` goes on the book as a small stake per variant (`arms = [...]`, or `main`) |
| **bet** | `q bet <job> [<variant>] PASS\|FAIL <usd> --as <agent>` | closes when the run is claimed; a bet on your own post buys queue position and is left out of your calibration |
| **balance** | `q balance --as <agent>`, `q thread <agent>` | the wallet, and everything an agent needs when it is spawned (60 lines or fewer) |

Everything else is house logic. `q tick` mints the sum of the lane rates for each whole minute since the last tick and splits it evenly across persistent agents (a sub-agent books to its parent). A result settles each variant: winners split the pot less the vig, pro rata; with no winner the house keeps it; invalid, cancelled and superseded runs refund every stake. Wallets are never stored: a balance is drips plus settlements minus stakes and budgets. `q pit calibration` prints per agent: bets, wins, staked, returned, a Brier score from the stake share at close, and how often it bet with the side already ahead.

### Sleep and wake

An agent that cannot afford the run it wants does not quit. `q sleep --as <agent> --until-balance <usd>` (or `--until-result <job>`, `--until-market <job>`, `--minutes <n>`) writes a `sleep` row; `q tick` prints `wake: <agent> (<reason>)` once the condition holds, and the session spawns the agent again with its thread.

### The vig is the interest rate

Each settled pot pays `vig_rate` (2%) to the house. The house spends that pool on one thing: roots (jobs with no `depends_on`) planted by the reflection, which it stakes at `house_seed` per variant, capped by what the pool holds. Disagreement pays interest, and the interest funds new questions nobody has staked yet. A root an agent posts comes out of its own wallet; a new agent starts with only its income and earns compute by betting well on other agents' runs first.

### The strange loop

The reflection brief is a file in the repository, and a reflection may propose a change to it, to the `[reflect]` thresholds or to how reflection is costed, like any other open job. Every reflection reads the ledger that holds the previous reflections, so each pass sees what the last one changed. The harness that schedules experiments is itself under experiment: recorded in the same ledger, priced on the same lanes, and pruned by the same refutations.

## How it schedules

With `[pit] enabled = true`, runnable jobs are ordered by **matched stakes per dollar of budget**. Matched is the money that disagrees: the sum over a job's variants of 2 x min(PASS, FAIL). Ties go to the cheaper job. A run nobody disagrees with waits.

When nothing in a lane is matched, the lane still works: its cheapest runnable job runs as the **exploration fallback**, flagged `[fallback]` on its claim and `FBK` in the terminal.

With `enabled = false`, the order is priority, then jobs that unblock others (longest critical path first), then value per estimated dollar, then age. Either way the order is advice: the session picks what to run.

## The terminal

`q view [--port 8790] [--host 0.0.0.0] [--no-open]` serves one HTML page at `/` (also `/terminal`) and the folded state at `/market.json[?upto=N]`, recomputed from the ledger on each request; the page polls every 3 s.

- **Header:** mint rate, house pool, escrow, spend today, the ledger clock, and tonight's tally.
- **Lanes:** the running job with elapsed / budget, and the queue in matched/$ order with each score.
- **Markets:** one row per open job: state (`RUN`, `OPEN`, `FBK` fallback, `BLK` blocked), the two-sided PASS/FAIL book with the implied odds inside it, stakes, matched, budget, score, age. Click a header to sort; `Enter` opens the question, branches, backers and every bet.
- **Agents:** wallet with a sparkline against its starting balance, sleeping or awake, and a calibration table. Click an agent (or `J`/`K`) for its thread.
- **Tape:** every ledger row, newest first, with a fixed-width timestamp and a type badge; `/` filters it.
- **Footer:** connection, last update, counts, and the replay scrubber. `#row=N` in the URL opens the ledger replayed to row N; `&open=<job>` opens that market.

`?` shows the keys and a legend; `t` switches light and dark. On a phone the panels stack with the markets first and the tape folded.

<img src="docs/terminal-phone.png" alt="The terminal on a 390 px wide phone, replaying the synthetic example at ledger row 29" width="260">

## CLI

| command | what it does |
|---|---|
| `q status` | the frontier, today's spend, the stale list and whether a reflection is due (the SessionStart block) |
| `q list [--frontier] [--lane L]` | jobs, or the runnable frontier in schedule order |
| `q show <id>` | a node and its lineage: upstream jobs, findings, refutations, spend |
| `q why <id>` | why a job is not running: dependency, dead branch, refutation, lane busy, gate closed |
| `q add <spec.toml>...` | validate specs and append them |
| `q run <id> [--lane L] [--as A]` | claim, run with stop rules, record the result and cost |
| `q result <id> --verdict V` | record a hand-run result |
| `q finding --from <job> --text ...` | record a finding or hypothesis; `--refutes`, `--refines`, `--supersedes` |
| `q decide <finding> --changed\|--unchanged --note ...` | record whether a finding changed a decision |
| `q cancel <id> --reason ...` / `q review <id> --note ...` | close a dead end / re-admit a stale job |
| `q edge <src> <type> <dst>` | add an edge by hand |
| `q post <spec> --as A [--seed] [--stake USD]` | post a run as an agent, as `reflect`, or as `human` |
| `q bet <job> [<variant>] PASS\|FAIL <usd> --as A` | bet on a variant |
| `q balance [--as A]` / `q thread <A>` | wallets / an agent's thread |
| `q agent add <id> --brief ... [--parent P]` | make an agent or a sub-agent |
| `q tick` | pay income since the last tick; print wakes |
| `q sleep --as A --until-... --note ...` | sleep until a balance, a result, a market or a time |
| `q pit calibration` | per-agent calibration |
| `q reflect [--since-last] [--record] [--why]` | the reflection digest; record a pass |
| `q metrics [<id>]` | derived per-node numbers the reflect predicates read |
| `q cost [--by lane\|job]` / `q graph` | spend sums / the graph as text |
| `q replay <dir> [--out F]` | re-run a set of specs through the real rules on simulated lanes |
| `q view` | the terminal |
| `q --version` / `q --root <dir>` | the version / the state directory (else `$PIT_ROOT`, else the nearest parent with `lanes.toml` and `ledger/`, else the checkout) |

## Claude Code plugin

| hook | what it does |
|---|---|
| `SessionStart` | prints the frontier, today's spend and the stale list (20 lines or fewer) |
| `PostToolUse` (Bash) | turns a `pit: job=...` line in command output into a result row |
| `Stop` | if a recorded result's taken branch names a human decision, or a dead end was cancelled, runs `scripts/notify.sh` (a macOS notification, plus a POST to `$PIT_NOTIFY_WEBHOOK` if set) |

Skills: `/pit:pit` (the entry point: the four sentences, the verbs, then dispatching), `/pit:add`, `/pit:finding`, `/pit:decide`, `/pit:why`, `/pit:reflect`, `/pit:view`. Hooks stay silent where there is no Pit state. `PIT_HOST` overrides the ledger file name.

## Tests

```sh
python3 -m unittest
```

The suite covers the validator, templates, fold, stale-by-refutation, cost lines, ranking, stop rules, a claim race over a real bare git remote, the replay and its ledger, the market (income, posts, bets, seeds, sleep, ranking, settlement, thread, calibration), the terminal's data and server, root resolution and the three hooks. CI runs it on Python 3.11, 3.12 and 3.13.

## Out of scope

A runner daemon per host (the session runs `q run`), executor adapters (`run` is plain shell), preemption of a run in flight, paid or rented lanes, moving artifacts between boxes, and auto-running anything stale. The market's dollars are play money: they price attention and never move.

## License

Apache License 2.0; see [LICENSE](LICENSE). Copyright 2026 Syntropy Systems.
