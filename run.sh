#!/usr/bin/env bash
set -eo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$project_dir${PYTHONPATH:+:$PYTHONPATH}"
# Preserve the local JSON experiment CLI; official calls use two directories.
if [[ "${1:-}" == --* ]]; then
  exec "${PYTHON:-python3}" -m agent.cli agent "$@"
fi
exec "${PYTHON:-python3}" -m agent.official_io "$@"
