#!/bin/sh
# A reference adapter: run a command, then print the one report line Pit reads.
#   run.sh <command...>
# Exit 0 of the command = pass, anything else = fail. Meters here are illustrative: a real adapter reads its bench's own
# output (token counts from an API response, rows scanned, GPU memory-hours) and names them as meters. Under a runner,
# the whole request's wall_s (printed after this line) replaces the one printed here.
# The command's output streams through line by line (tee), never held in a variable: a run killed for funding still
# leaves its transcript in the run log. The report line is parsed from the tee'd copy.
start=$(date +%s)
tmp=$(mktemp); trap 'rm -f "$tmp" "$tmp.rc"' EXIT
{ "$@" 2>&1; echo $? >"$tmp.rc"; } | tee "$tmp"       # POSIX sh has no pipefail: the command's exit code goes to a file
rc=$(cat "$tmp.rc")
lines=$(wc -l <"$tmp" | tr -d ' ')
bytes=$(wc -c <"$tmp" | tr -d ' ')
verdict=pass; [ "$rc" -eq 0 ] || verdict=fail
echo "pit: verdict=$verdict wall_s=$(( $(date +%s) - start )) meters={\"out_lines\": $lines, \"out_bytes\": $bytes} result={\"rc\": $rc}"
