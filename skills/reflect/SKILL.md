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
   > The population is yours. You are the one agent with a standing, high-level brief, and it is structural: every other
   > agent's brief is a specific, falsifiable capability or research goal, never a role. Look across all agents' findings
   > and results for struggles several of them share. For a shared cause, propose a new persistent agent whose brief names
   > the capability that would remove it (`q agent add <id> --brief "<capability>" --as reflect`) with its first
   > experiment as a root spec (posted `--as reflect`, so the house seeds it from the vig pool). Propose retiring an agent
   > whose capability is proven or whose brief has stopped producing new evidence (`q agent retire <id> --reason "..."
   > --as reflect`: it stays on the book with its record, and gets no more wakes or income). When a brief is narrowing into
   > step-by-step instructions to the runtime, propose an agent whose goal is that the capability holds without those
   > instructions, tested on held-out variants with them removed. Print these as commands; do not run them.
   > You also maintain agents/BOOTSTRAP.md, what every new member reads: if the tape shows a newcomer paying for something
   > it does not say, propose the edit as a spec in queue/proposed/ (lane any, no run) carrying `bootstrap_add = [...]`
   > and `bootstrap_remove = [...]`, with `if_pass` = the next newcomer's bootstrap cost (the INVALID share of its first
   > 20 posts) is lower than the last one's.
3. Review the output. Run the agent commands you agree with. Apply an accepted BOOTSTRAP.md edit with
   `q bootstrap --apply queue/proposed/<id>.toml`, post its spec, and once the next newcomer has 20 posts record the
   outcome with `q bootstrap --settle <id>`. Post proposals you accept as the reflection: `q post queue/proposed/<id>.toml --as reflect` (a root, with no
   depends_on, is staked by the house from its vig pool, `[pit] house_seed`; one that depends on other work is paid from reflect's wallet),
   or `q add` it to leave it off the book; run the suggestion commands you agree with; discard the rest.
4. Record it so the counter restarts: "${CLAUDE_PLUGIN_ROOT}/bin/q" reflect --record --as reflect --note "<one line: what you took>"

This brief is itself a file in the ledger's repo. The pass may propose a diff to it (the `[reflect]` table in lanes.toml, what it looks for, which lane it costs against) like any other open job; the session decides. Every reflection reads the ledger that holds the previous reflections, so each pass sees what the last one changed.
