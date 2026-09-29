---
name: reflect
description: Pit reflection - when the trigger fires (any combination of the `[reflect]` predicates in lanes.toml: rows, hours, after a row type, spend rising), have one Opus subagent look for patterns across the jobs and findings (savings, obvious ideas, open jobs to change) and propose new jobs. Use when status says "reflect due", or for "pit reflect".
---
# Reflect

Proposals are suggestions; you (the session) decide. Nothing is cancelled or added automatically.

1. Run "${CLAUDE_PLUGIN_ROOT}/bin/q" reflect --since-last  (a plain-text digest: jobs, findings, decisions, cancels, spend by lane, frontier, open specs).
2. Spawn ONE subagent (Agent tool, `model: opus`) with the digest and this brief. Reflection has an identity in the Pit: `reflect`
   (register it once: `q agent add reflect --brief "reflection passes: patterns across jobs and findings"`); each pass is a sub of it
   (`q agent add reflect-<yyyymmddThhmm> --parent reflect --brief "<this pass>"`), so its bets are booked to `reflect` and scored on its record.
   > Bet as the reflection: where the digest gives you a reason to expect PASS or FAIL on an open job, place it with
   > "${CLAUDE_PLUGIN_ROOT}/bin/q" bet <job> [<variant>] PASS|FAIL <amount> --as <your reflect sub> and say the reason in one line.
   > Look for patterns the session is too close to see. Savings: repeated waits, jobs whose pass/fail led to the same next action,
   > lanes idle while another queues, budgets consistently over or under. Ideas that are obvious across tasks. Open jobs whose
   > question, expect, budget or lane should change. Output: (a) at most 8 observations, each citing the ledger rows (job/finding ids)
   > it rests on; (b) proposed new jobs as complete TOML specs written to queue/proposed/<id>.toml (see the pit:add skill for the
   > format; never run `q add`); (c) suggestions on open jobs as `q finding --kind hypothesis --from reflect --text "..."` commands,
   > printed, not run. Also flag: results recorded on a dead `@pass`/`@fail` branch (`q result` bypasses the gate); specs or controls whose question or branch text encodes a surface/parity rule older than the latest upstream change; finding-driven cancels or supersedes with no `q decide` row; work that exists only in a temp directory or an uncommitted worktree; lanes idle while the frontier sits on one lane; findings or hypotheses with 0 open dependents whose text names a next step; open jobs whose depends_on points at a superseded/invalid node (permanently blocked); controls that predate an upstream surface change; which previous F:reflect-* rows were acted on (close the rest in one closures row). Also report $ per decision by lane, and which spend a $0 code or log read could have replaced.
3. Review the output. Post proposals you accept as the reflection: `q post queue/proposed/<id>.toml --as reflect` (a root, with no
   depends_on, is staked by the house from its vig pool, `[pit] house_seed`; one that depends on other work is paid from reflect's wallet),
   or `q add` it to leave it off the book; run the suggestion commands you agree with; discard the rest.
4. Record it so the counter restarts: "${CLAUDE_PLUGIN_ROOT}/bin/q" reflect --record --as reflect --note "<one line: what you took>"

This brief is itself a file in the ledger's repo. The pass may propose a diff to it (the `[reflect]` table in lanes.toml, what it looks for, which lane it costs against) like any other open job; the session decides. Every reflection reads the ledger that holds the previous reflections, so each pass sees what the last one changed.
