#!/bin/sh
# Run Python >= 3.11 (tomllib) with pit importable, whatever `python3` is on this box.
HERE=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONPATH="$HERE${PYTHONPATH:+:$PYTHONPATH}"
for py in python3.13 python3.12 python3.11 python3; do
  if command -v "$py" >/dev/null 2>&1 && "$py" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
    exec "$py" "$@"
  fi
done
exec uv run --no-project --python 3.11 python "$@"
