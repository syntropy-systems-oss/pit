# Changelog

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
