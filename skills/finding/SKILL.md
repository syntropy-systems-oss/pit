---
name: finding
description: Record a Pit finding or hypothesis, optionally refuting/refining/superseding another (q finding). Use when a run or a read produced a claim, or "pit finding".
---
# Record a finding

"${CLAUDE_PLUGIN_ROOT}/bin/q" finding --from <job-id> --id F:<short-name> --text "<the claim, one sentence, with its evidence>" [--refutes <id>] [--refines <id>] [--supersedes <id>]

- `--kind hypothesis` for an open claim that jobs will test.
- `--refutes` marks the target refuted and everything downstream of it STALE (queued jobs stop being runnable; running
  ones are flagged). The command prints what went stale: read each with "${CLAUDE_PLUGIN_ROOT}/bin/q" show <id>, then review or cancel it.
- Findings, not runs, are what later jobs depend on. Choose ids that name the claim.
