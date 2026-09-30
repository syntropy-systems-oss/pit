# The runner

A resource is any box running `q runner`. Scaling is adding one: start a runner on the new box, add a lane for it in
`lanes.toml` with its price and `url`, and the scheduler starts sending it work. A lane without `url` keeps the default:
`q run` forks the job on the host that runs it.

## Run it

The runner is a native process, the standard library's `http.server` and nothing else:

```sh
q runner --port 8791 --slots 1 --workdir ~/.pit-runner                  # loopback only
PIT_RUNNER_TOKEN=... q runner --host 0.0.0.0 --port 8791                  # reachable from other hosts: set a token
```

Run it under the box's service manager so it comes back after a reboot: a launchd agent on macOS
(`ProgramArguments` = the path to `bin/q`, `runner`, `--port`, `8791`; `KeepAlive` true), or a systemd unit on Linux
(`ExecStart=/path/to/pit/bin/q runner --host 0.0.0.0 --port 8791`, `Restart=always`, the token in
`EnvironmentFile=`). Native matters where the work needs the host's hardware directly, such as a GPU a container cannot
reach. On a Linux box where containers are the norm, `Dockerfile.runner` wraps the same process; it is optional.

With a token set (`--token-env`, default `PIT_RUNNER_TOKEN`), every request needs `Authorization: Bearer <token>`; the
dispatcher sends `$PIT_RUNNER_TOKEN` when it is set. Anyone who can reach the port with the token can run commands as
the runner's user: bind to loopback or a private network, and run it as a user that can touch only what the work needs.

## The lane

```toml
[lanes.ci]
usd_per_h = 100
url = "http://ci-box:8791"
gate = "curl -sf -m 3 http://ci-box:8791/health"      # 200 while a slot is free
repo = "https://example.com/you/project.git"          # optional: run jobs at a git ref of this repo
base = "main"                                         # the default ref; a spec's own `ref` wins
```

## The protocol

`POST /run` with a JSON body:

| key | |
|---|---|
| `job` | the job id |
| `command` | a shell command, run with `sh -c` in the runner's own working directory for that slot; or instead |
| `repo`, `ref`, `script` | a repository, a ref (a branch, a tag, a sha, or `refs/pull/N/head`) and a shell command to run in a checkout of it at that ref |
| `funded_s` | seconds the funding buys: the run is killed at this many seconds after the request arrived |
| `base` | comparison ref for the diffstat (defaults to `ref` for direct protocol callers) |
| `env` | extra environment: the dispatcher sends `PIT_JOB`, `PIT_LANE`, `PIT_FUNDED_S` |

The reply is `text/plain`, streamed line by line as the command prints (stdout and stderr merged). The dispatcher reads
it exactly as it reads a forked process: the command's own `pit: verdict=... meters={...} result={...}` line is the
result, and the stop rules in the spec's `fail_on` apply to every line (a hit closes the connection, which kills the
run). The runner ends every reply with one more line, `pit: wall_s=S` (or `pit: stop=timeout wall_s=S` after a kill),
where `S` is the whole request's wall: fetching, building and running. That is the wall the lane's `usd_per_h` is
charged on, so a slow build is paid for where everyone can see it.

`GET /health` answers 200 `{"free": n}` while a slot is free and 503 when none is. `--slots K` runs up to K requests at
once; a request with no free slot gets 503, and the job books INVALID naming the runner.

## Checkouts, not builds

With `repo`, the runner keeps one checkout per repository and slot under `<workdir>/checkouts/`. The first request
clones it; every request after that runs `git fetch --force origin <ref>`, `git checkout --force --detach FETCH_HEAD` and
`git clean -fd`. Untracked files go; files the repository ignores (build outputs, caches) stay. A fetch that fails books
INVALID with git's message.

The runner never builds anything. Building, and deciding what can be reused, is the script's business, because only the
script knows what its build depends on. The pattern that keeps it cheap:

1. Fingerprint the sources the build reads (for example `git rev-parse HEAD:<dir>`, or a hash of the lockfile and the
   source tree).
2. Look for an artifact under that fingerprint, in an ignored directory of the checkout or in `$PIT_CACHE` (the runner
   sets it to `<workdir>/cache`, shared by every slot).
3. Build only when it is missing, and store the result under the fingerprint.

A first build on a new box is slow and the ledger shows it: the wall is charged on the lane. The next run on the same
sources reuses it.

Repo runs also substitute `{tree}` in the script, set `PIT_TREE` and `PIT_REF`, and return the tested commit, comparison commit and diffstat as `tree={...}` on the final report line. The dispatcher records these on the result. `base` replaces the old lane-level `ref` default; spec and protocol `ref` still select the proposed commit.
