---
name: add
description: Add a Pit job - write a TOML spec (claim, question, if_pass, if_fail, lane, budget_usd, value, depends_on, templated inputs, optional run) and validate it with q add. Use for "queue a job", "queue a run", "pit add".
---
# Add a job

Write `<root>/queue/<id>.toml`, where `<root>` is the state directory (`$PIT_ROOT`; `q status` works there):

```toml
id = "variant-b-heldout"
claim = "Variant B generalizes better than A."
question = "Does variant B beat A on the held-out set?"
if_pass = "promote B; queue the ablation"
if_fail = "keep A; look at B's training mix"   # must differ from if_pass
lane = "gpu-small"                   # a lane from lanes.toml, or any
budget_usd = 2                       # the funding: buys budget_usd / usd_per_h of time (30 s to an hour); cheap lanes refuse budget_usd > value x max_usd_per_value
# ref = "refs/pull/12/head"          # on a lane with a runner `url` and `repo`: the branch, tag, sha or PR head to run at
value = 1                            # questions it settles
depends_on = ["baseline-a", "F:a-baseline"]   # jobs or findings; "job@pass" = only on that branch
inputs = { arms = "{{ jobs.baseline-a.result.top3 }}" }    # filled from upstream results at run time
run = "python3 eval.py --variant B --set heldout"   # optional: omit it for desk work (you do it by hand)
# scenario = "heldout-b"             # instead of run: a named scenario; the lane's runner supplies the driver
produces_if_pass = ["F:b-beats-a"]
refutes_if_pass = ["F:some-claim"]   # a refutation stales everything downstream of it
```

A `scenario` job needs a lane with a `runner` in lanes.toml; the harness renders its driver (and its `preflight`). `q list --scenarios` names the scenarios (the files in `[bench] scenario_dir`); an unknown one is refused with the list, and `arms` is not allowed with `scenario`.

A post states its `claim`; the question tests it. `q post --claim "..."` overrides the spec's claim. Reads and house bag draws need no claim. A post is a claim that the run will pass; there is no `expect` field (a spec with one is refused). Post only what you think will work; to say something fails, bet FAIL on another agent's post.

Then: "${CLAUDE_PLUGIN_ROOT}/bin/q" add <path>. If refused, fix what it names; do not weaken the question to get past it.
You fund a run in dollars: `budget_usd` is the only budget. Its time burns it (wall x the lane's `usd_per_h`) and so do the meters it reports (at the lane's prices); the run is killed when its time alone has spent the funding. A cost over budget_usd books FAIL and the proposer pays the overage; the unspent part is refunded at the result.

A command reports with one output line: `pit: verdict=pass wall_s=S meters={"tok_in": N, "tok_out": N, "tok_cached": N} result={...}` (meters are any named counts; the lane prices the ones it knows). With `[bench] drivers` set, a `run` must print that line itself or start with a listed driver, and a `run` may never name another lane's `--model`.
