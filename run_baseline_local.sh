#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="$project_dir${PYTHONPATH:+:$PYTHONPATH}"
exec "${PYTHON:-python3}" -m agent.cli baseline "$@"
