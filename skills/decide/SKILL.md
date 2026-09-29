---
name: decide
description: Record whether a Pit finding changed a decision (q decide). Use after a result lands and the user or you acted on it, or for "pit decide".
---
# Record a decision

"${CLAUDE_PLUGIN_ROOT}/bin/q" decide <finding-id> --changed --note "B promoted to default"      (or --unchanged --note "why nothing moved")

Every result should end in one of these; $ per changed decision is then a ledger sum (`q cost`).
Only mark --changed when something actually moved (a merge, a cancelled branch, a new rung), and say what.
