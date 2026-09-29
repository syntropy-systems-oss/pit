#!/bin/sh
# notify.sh "<one-line summary>": a macOS notification here, plus a POST to $PIT_NOTIFY_WEBHOOK if set
# (a chat-webhook body, {"content": ...}). The summary carries job ids, verdicts and the spec's own branch text only.
msg=$(printf '%s' "$1" | tr '\n' ' ' | cut -c1-400)
if command -v osascript >/dev/null 2>&1; then
  esc=$(printf '%s' "$msg" | sed 's/\\/\\\\/g; s/"/\\"/g')
  osascript -e "display notification \"$esc\" with title \"Pit\"" >/dev/null 2>&1
fi
if [ -n "$PIT_NOTIFY_WEBHOOK" ]; then
  body=$(printf '%s' "$msg" | sed 's/\\/\\\\/g; s/"/\\"/g')
  curl -sf -m 10 -H 'Content-Type: application/json' -d "{\"content\": \"$body\"}" "$PIT_NOTIFY_WEBHOOK" >/dev/null
fi
exit 0
