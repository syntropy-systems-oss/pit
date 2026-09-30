#!/bin/sh
# A reference adapter: run a command, then print the one report line Pit reads.
#   run.sh <command...>
# Exit 0 of the command = pass, anything else = fail. Meters here are illustrative: a real adapter reads its bench's own
# output (token counts from an API response, rows scanned, GPU memory-hours) and names them as meters. Under a runner,
# the whole request's wall_s (printed after this line) replaces the one printed here.
start=$(date +%s)
out=$("$@" 2>&1); rc=$?
printf '%s\n' "$out"
lines=$(printf '%s\n' "$out" | wc -l | tr -d ' ')
bytes=$(printf '%s' "$out" | wc -c | tr -d ' ')
verdict=pass; [ "$rc" -eq 0 ] || verdict=fail
echo "pit: verdict=$verdict wall_s=$(( $(date +%s) - start )) meters={\"out_lines\": $lines, \"out_bytes\": $bytes} result={\"rc\": $rc}"
