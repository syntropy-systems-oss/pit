# Changelog

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
