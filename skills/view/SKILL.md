---
name: view
description: Open the Pit terminal (lanes, open markets with the PASS/FAIL book, agents, calibration, the tape, a replay scrubber) in the browser. Use for "pit view", "open the terminal", "show me the market", "what's happening".
---
# View

Run in the background (do not wait on it):

nohup "${CLAUDE_PLUGIN_ROOT}/bin/q" view --no-open > "${TMPDIR:-/tmp}/pit-view.log" 2>&1 &

If port 8790 is taken a terminal is probably already running: `curl -s localhost:8790/market.json | head -c 100`. Then give the user the URL, http://localhost:8790/ (add `--port N` for another). The page polls `/market.json` every 3 s; `#row=N` in the URL opens it replayed to ledger row N; `?` shows the keys.
