# Contributing

- Python 3.11+ and the standard library only. `scripts/py.sh` finds a suitable interpreter.
- Run the tests before a pull request: `python3 -m unittest` (they use `lanes.example.toml` and `examples/replay-synthetic/`).
- The ledger is append-only. A change that needs new state adds a row type; it never rewrites old rows. `fold()` must keep
  reading every ledger written before the change.
- Keep `q` a recorder and a runner. Decisions belong to the session (or the market); a feature that decides for it needs a
  strong case in the pull request.
- If you change `examples/replay-synthetic/`, regenerate its ledger: `bin/q replay examples/replay-synthetic --out examples/replay-synthetic/ledger/replay.ndjson`.
- Never commit a real state directory (`lanes.toml`, `ledger/`, `queue/`, `agents/`): it belongs in its own repository.
- The terminal is one file, `view/terminal.html`, with no build step and no dependencies; it reads only `/market.json`. Screenshots in
  `docs/` are taken from the synthetic example only (`PIT_ROOT=examples/replay-synthetic bin/q view`, then `/#row=29&open=variant-b`
  at 1440x900 and `/#row=29` at 390x844), never from a real ledger.
