#!/usr/bin/env bash
# Resident inference only. The submission API is started separately.
set -eo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export PYTHONPATH="$project_dir${PYTHONPATH:+:$PYTHONPATH}"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export HIP_VISIBLE_DEVICES="${HIP_VISIBLE_DEVICES:-0}"
[[ "$HIP_VISIBLE_DEVICES" != *,* ]] || { echo 'Exactly one GPU must be visible' >&2; exit 2; }
export AGENT_TMP="${AGENT_TMP:-/var/tmp/rtl_agent}"
export EDA_TMP="${EDA_TMP:-/var/tmp/rtl_eda}"
export RTL_AGENT_VIVADO_VERSION="${RTL_AGENT_VIVADO_VERSION:-2025.2}"
export LLM_BASE_URL="${LLM_BASE_URL:-http://127.0.0.1:8000/v1}"
export MODEL_PATH="${MODEL_PATH:-$project_dir/model/weights}"
mkdir -p "$AGENT_TMP" "$EDA_TMP"
exec 9>"$AGENT_TMP/start.lock"
flock -w 30 9
cd "$project_dir"
probe() { "${PYTHON:-python3}" -c 'from serve.serve_api import probe_health; raise SystemExit(0 if probe_health()["ready"] else 1)' >/dev/null 2>&1; }
if probe; then exit 0; fi
"${PYTHON:-python3}" serve/start.py --check
if [ -f "$AGENT_TMP/vllm.pid" ] && kill -0 "$(cat "$AGENT_TMP/vllm.pid")" 2>/dev/null; then
  pid="$(cat "$AGENT_TMP/vllm.pid")"
else
  setsid "${PYTHON:-python3}" "$project_dir/serve/start.py" >"$AGENT_TMP/vllm.log" 2>&1 </dev/null 9>&- &
  pid=$!
  echo "$pid" >"$AGENT_TMP/vllm.pid"
fi
until probe; do
  kill -0 "$pid" 2>/dev/null || { echo "vLLM exited; see $AGENT_TMP/vllm.log" >&2; exit 1; }
  if [ "$SECONDS" -ge "${START_TIMEOUT_S:-900}" ]; then
    echo "vLLM readiness timed out; see $AGENT_TMP/vllm.log" >&2
    exit 1
  fi
  sleep 2
done
