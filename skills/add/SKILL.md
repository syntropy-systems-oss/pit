---
name: add
description: Add a Pit job - write a TOML spec (question, expect, if_pass, if_fail, lane, budget, value, depends_on, templated inputs, optional run) and validate it with q add. Use for "queue a job", "queue a run", "pit add".
---
# Add a job

Write `<root>/queue/<id>.toml`, where `<root>` is the state directory (`$PIT_ROOT`; `q status` works there):

```toml
id = "variant-b-heldout"
question = "Does variant B beat A on the held-out set?"
expect = "pass"                      # pass | fail: your prediction
if_pass = "promote B; queue the ablation"
if_fail = "keep A; look at B's training mix"   # must differ from if_pass
lane = "gpu-small"                   # a lane from lanes.toml, or any
budget_s = 300                       # hard stop at 2x -> INVALID
budget_usd = 9                       # cheap lanes refuse budget_usd > value x max_usd_per_value
value = 1                            # questions it settles
depends_on = ["baseline-a", "F:a-baseline"]   # jobs or findings; "job@pass" = only on that branch
inputs = { arms = "{{ jobs.baseline-a.result.top3 }}" }    # filled from upstream results at run time
run = "python3 eval.py --variant B --set heldout"   # optional
produces_if_pass = ["F:b-beats-a"]
refutes_if_pass = ["F:some-claim"]   # a refutation stales everything downstream of it
```

Then: "${CLAUDE_PLUGIN_ROOT}/bin/q" add <path>. If refused, fix what it names; do not weaken the question to get past it.
A command reports cost with one output line: `pit: verdict=pass uncached=N cached=N out=N wall_s=S result={...}`.
