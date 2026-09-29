---
name: why
description: Explain why a Pit job is not running - unmet dependency, dead branch, refutation, lane busy or gate closed (q why-blocked). Use for "why is X blocked", "pit why".
---
# Why is it blocked?

"${CLAUDE_PLUGIN_ROOT}/bin/q" why-blocked <id>

Reads: "waiting on X" (an upstream job or finding not settled, followed down the chain), "X ended fail; runs only on pass"
(a dead branch: cancel it), "stale: F refuted by J" (review or cancel), "lane busy", "gate closed" (the lane's `gate`
command in lanes.toml exited non-zero, e.g. a box that admits bench work only while production is idle). Then "${CLAUDE_PLUGIN_ROOT}/bin/q" show <id> for its lineage.
