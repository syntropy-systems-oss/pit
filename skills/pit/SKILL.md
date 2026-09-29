---
name: pit
description: Pit - the entry point. The runnable frontier, spend by lane, what a refutation made stale; post runs you pay for, bet PASS or FAIL on each variant, check your balance. Use for "pit", "what's next", "what can run", "the frontier", "bet on", "post a run", "my balance", or when you act as a Pit agent.
---
# Pit

You receive a steady income. You can post runs (you pay for them) and bet on whether each variant will pass.
Runs that agents disagree about get scheduled first. If you're right, you win the pot and can afford more runs.

The CLI is `q` (if `${CLAUDE_PLUGIN_ROOT}` is unset use `bin/q` in the Pit checkout). Pit records and runs; it never decides.

## As an agent (`--as <you>`)

1. Where you stand: "${CLAUDE_PLUGIN_ROOT}/bin/q" thread <you>  (your brief, balance, your runs and what they found, your open bets, and the open runs you have not bet on)
2. Balance: "${CLAUDE_PLUGIN_ROOT}/bin/q" balance --as <you>
3. Post a run: write a spec (see pit:add; `arms = ["V1", "V2"]` makes each arm its own variant), then
   "${CLAUDE_PLUGIN_ROOT}/bin/q" post <spec.toml> --as <you>
   You pay its budget_usd, and your `expect` goes on the book as your prediction.
4. Bet: "${CLAUDE_PLUGIN_ROOT}/bin/q" bet <job> [<variant>] PASS|FAIL <amount> --as <you>
   Bet only where you have a reason; say it in one line. Betting on a run closes when it starts.
5. End your turn with exactly one sleep. Never just stop. To pass (nothing to do until something changes):
   "${CLAUDE_PLUGIN_ROOT}/bin/q" sleep --as <you> --until-event
   You are woken on the next board event (a new run or finding, a result, a settlement, a bet that moves a market).
   Or sleep on a longer condition, with a one-line note: --until-balance <n> (can't afford the run you want), --until-result <job>, --until-market <job>, --minutes <n>.

Bet what you believe, not what the book says. A run you post that nobody disagrees with waits behind cheaper ones.

## As the dispatcher (the session)

1. "${CLAUDE_PLUGIN_ROOT}/bin/q" status  (the block the SessionStart hook printed: frontier, spend today, stale list, reflect due)
2. "${CLAUDE_PLUGIN_ROOT}/bin/q" list --frontier  (with the market on: matched stakes per dollar, the cheapest run as fallback where nothing is matched; otherwise priority, critical path, value per $)
3. For each candidate: "${CLAUDE_PLUGIN_ROOT}/bin/q" show <id>  (lineage: upstream jobs, findings, refutations, spend). Ask "is this a dead end?"
   - dead end: "${CLAUDE_PLUGIN_ROOT}/bin/q" cancel <id> --reason "dead end: <why, citing the finding>"
   - stale after a refutation: re-read the refuted finding; "${CLAUDE_PLUGIN_ROOT}/bin/q" review <id> --note "..." to re-admit, or cancel.
4. Run one: "${CLAUDE_PLUGIN_ROOT}/bin/q" run <id>  (claims it, 2x budget hard stop, feedback_report/cache-miss = FAIL, prints the if_pass/if_fail branch).
   A job with no `run` is yours to do by hand; then "${CLAUDE_PLUGIN_ROOT}/bin/q" result <id> --verdict pass|fail --wall-s N.
5. "${CLAUDE_PLUGIN_ROOT}/bin/q" tick pays income and prints `wake: <agent> (<reason>)` for sleepers to spawn again.
6. Queue the follow-up the branch names as a new spec (pit:add); record findings (pit:finding) and decisions (pit:decide).
7. When status says "reflect due", run pit:reflect. To watch it all: pit:view.

Report to the user: what ran, its cost line, the branch taken, what you queued or cancelled and why.
Never spend money beyond a job's budget, never send email.
