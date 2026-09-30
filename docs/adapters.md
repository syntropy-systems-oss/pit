# Adapters

Pit reads one line from a run's output:

```
pit: verdict=pass|fail|invalid wall_s=S meters={"tok_in": N, "tok_out": N, "tok_cached": N, "<name>": N} result={...}
```

Every key is optional and the last value for a key wins; `result=` comes last (the rest of the line is JSON, which
templated dependants read as `{{ jobs.<id>.result.<key> }}`). No verdict means INVALID: the run is not evidence.

A bench rarely prints that line itself. An adapter is the small script between the two: it runs the bench, reads the
bench's own output (a results file, a JSON summary, an API's usage block, an exit code) and prints the line. It decides
three things:

- **The verdict.** What PASS means for this bench: a threshold on a score, every case green, a regression absent. A
  harness error, where nothing meaningful ran, is `verdict=invalid`; say why on the line before it, so the tape names
  the cause.
- **The meters.** Every count the bench can report that costs something: tokens in, out and cached (`tok_in`,
  `tok_out`, `tok_cached`), requests, rows scanned, GPU memory-hours. Name them as they are; the lane's price table
  decides which ones cost money (`[lanes.<name>.prices]`, `usd_per_mtok_<x>` for `tok_<x>`, `usd_per_<name>` for the
  rest). An unpriced meter is still recorded.
- **The result.** The few numbers a follow-up job needs, not the whole report.

`examples/adapters/shell/run.sh` is the smallest one: it runs any command, turns its exit code into the verdict and
counts its output as two illustrative meters. Use it as a lane's `runner` template or in a `run`:

```toml
run = "examples/adapters/shell/run.sh make test"
```

The adapter for a real bench is private to each site: it knows that bench's output format, its thresholds and its
fixtures. Keep it in the bench's repository, next to the bench, and point the lane's `runner` template (or a job's
`run`) at it. On a lane with a runner `url` and `repo`, the adapter lives in the repository the runner checks out, so
every ref carries the adapter that matches it. If the bench needs a build, the adapter is also where build reuse lives
(see [runner.md](runner.md)).
