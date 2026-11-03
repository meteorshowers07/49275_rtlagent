#!/usr/bin/env bash
# serve/run_api.sh -- keep the submission API (serve_api.py) up.
#
#   ./serve/run_api.sh start     # start in the background, detached from this terminal
#   ./serve/run_api.sh stop      # stop the watchdog and the API
#   ./serve/run_api.sh status
#
# Why it exists: Radeon Cloud's rc-tunnel probes 127.0.0.1:7860 every 10 s and
# revokes the tunnel after about a minute of failed probes. The public URL is
# then gone for good, and a restarted service does not get it back. So the API
# must never be down for long: this script restarts it within a couple of
# seconds whenever it exits, and runs under setsid so closing the terminal or
# notebook that started it does not take it down.
#
# Start serve/start.sh (the inference service) first, then this, then
# rc-tunnel expose. FPGACHINA_TOKEN and MODEL_NAME must be set in the
# environment, e.g. by `source /workspace/env.sh`.
#
# Do not use `set -u` here, see docs/API_CONTRACT.md section 10.

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${API_PORT:-7860}"
STATE="${AGENT_TMP:-/var/tmp}/rtl_run_api"
PIDFILE="$STATE/run_api.pid"
LOG="$STATE/serve_api.log"
mkdir -p "$STATE"

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

case "${1:-}" in
start)
    if running; then
        echo "run_api.sh: already running (pid $(cat "$PIDFILE")), log $LOG"
        exit 0
    fi
    [ -n "$FPGACHINA_TOKEN" ] || { echo "run_api.sh: FPGACHINA_TOKEN is not set" >&2; exit 1; }
    setsid "$0" _loop </dev/null >>"$LOG" 2>&1 &
    sleep 1
    echo "run_api.sh: started (pid $(cat "$PIDFILE" 2>/dev/null)), log $LOG"
    ;;
_loop)
    echo $$ > "$PIDFILE"
    trap 'kill "$child" 2>/dev/null; wait "$child" 2>/dev/null; rm -f "$PIDFILE"; exit 0' TERM INT
    cd "$HERE/.."
    while :; do
        echo "$(date '+%F %T') run_api.sh: starting serve_api on 127.0.0.1:$PORT"
        "${PYTHON:-python3}" -m uvicorn serve.serve_api:app --host 127.0.0.1 --port "$PORT" --workers 1 &
        child=$!
        wait "$child"
        rc=$?
        echo "$(date '+%F %T') run_api.sh: serve_api exited (rc=$rc), restarting in 2 s"
        sleep 2
    done
    ;;
stop)
    if running; then
        kill "$(cat "$PIDFILE")"
        for _ in 1 2 3 4 5 6 7 8 9 10; do running || break; sleep 1; done
        echo "run_api.sh: stopped"
    else
        echo "run_api.sh: not running"
    fi
    ;;
status)
    if running; then echo "run_api.sh: running (pid $(cat "$PIDFILE")), log $LOG"
    else echo "run_api.sh: not running"; exit 1; fi
    ;;
*)
    echo "usage: $0 start|stop|status" >&2
    exit 2
    ;;
esac
