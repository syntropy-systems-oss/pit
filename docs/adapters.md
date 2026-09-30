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

**Stream, never buffer.** An adapter MUST pass its driver's output through line by line as it arrives, not collect
it in a shell variable (`out=$(cmd)`) and print it at the end. A run over its funding is killed where it stands; what it
printed up to then is its transcript, kept in the run log (`autopilot/logs/run-<job>-<stamp>.log`, on the result row as
`log`) and handed back to the proposer, whose last 40 lines arrive in the hand-back prompt. A buffering adapter killed
mid-run leaves a five-line log and nothing to learn from. Tee the output and parse the tee'd copy for the report:

```sh
# bash (or any sh with pipefail)
set -o pipefail
tmp=$(mktemp)
cmd 2>&1 | tee "$tmp"; rc=$?
# ... read the score, counts and usage out of "$tmp", then print the `pit:` line
```

Plain POSIX sh has no `pipefail`; `examples/adapters/shell/run.sh` writes the exit code to a file instead
(`{ cmd 2>&1; echo $? >"$tmp.rc"; } | tee "$tmp"`). A driver that block-buffers its own stdout when piped (Python,
most C programs) needs line buffering too: `PYTHONUNBUFFERED=1`, `python -u`, or `stdbuf -oL cmd`.

The adapter for a real bench is private to each site: it knows that bench's output format, its thresholds and its
fixtures. Keep it in the bench's repository, next to the bench, and point the lane's `runner` template (or a job's
`run`) at it. On a lane with a runner `url` and `repo`, the adapter lives in the repository the runner checks out, so
every ref carries the adapter that matches it. If the bench needs a build, the adapter is also where build reuse lives
(see [runner.md](runner.md)).


## Running a change

On a local lane, `repo = "~/src/project"` and `base = "main"` mean every run gets a fresh detached worktree at the posted commit (or base when no ref is posted). `base` defaults to `HEAD`. The command runs with cwd at the tree even if the spec has `cwd`; `{tree}` in `runner` or `run` expands to its shell-quoted path. Use it unquoted in templates, or use `"$PIT_TREE"` in shell. `PIT_REF` is the resolved commit, and `PIT_REF_NAME` is the posted spelling, if any. `PIT_ROOT` still points to the state directory: an adapter kept there can be invoked as `bash "$PIT_ROOT/adapters/run.sh" {scenario}`. With no `repo`, cwd behavior is unchanged.

Pit removes local trees after completion, timeout, stop rules, SIGINT, SIGTERM and SIGHUP. SIGKILL and host loss cannot run cleanup handlers. Put reusable build artifacts outside the disposable tree and key them by source fingerprints. The adapter owns preparation of ignored inputs and reports their provenance; they are not part of a commit. Never copy mutable inputs over files tracked at the proposed ref.

`q post --ref <name>` (or a spec's `ref`) pins a full commit SHA, the typed `ref_name`, and `base_ref`. `deny_paths` is an ordered list of case-sensitive globs matched against repository-relative paths in `base...ref`; `*` also matches slashes, and a later `!glob` allows an exception. Renames check both names. Protect the grader and driver, while leaving the product and intended scenario files editable. A remote URL repo is mirrored temporarily to perform the same post-time checks; the remote runner must be able to fetch the pinned SHA.

A configured scenario registry runs inside the proposed local tree at post time, with `PIT_TREE`, `PIT_REF` and `{tree}` available. This lets a change add a new scenario. `q list --scenarios` still queries the state root; sites can choose a baseline there when `PIT_TREE` is absent. Preflight remains a state-root check before dispatch, before the run tree exists.

Results store the tested `ref`, comparison `base_ref`, and `change` diffstat. `q list --changes` is the human's PR candidate queue: latest PASS results at a ref different from base. Blind betting views carry a change mark and question, never a diff. Nothing opens or merges a PR automatically.

For agent workspaces, set `[autopilot] workspace` to a path template with `{agent}` and `workspace_init` to a shell command with `{agent}` and `{path}`. These substitutions are shell-quoted, so leave them unquoted in the command. Init runs only for a missing path, before the first turn; subs reuse their wallet's tree. A failed init prevents that turn from spawning. Dry runs do not create trees. Both runtimes receive the workspace via `--add-dir`; Codex also receives its Git index and common object directories. The site must allow agents to inspect and commit their tree: Git status, diff, log, add, commit, switch/checkout, rev-parse, worktree list, stash and restore, plus its own editing and test tools.
