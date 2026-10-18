#!/usr/bin/env bash
# Launch the paper-trading dashboard (dashboard/paper-trading/trading_summary.py)
# as a detached Streamlit server on port 8501.
#
# Usage: ./stream-lit.sh [start|stop|restart|status]   (default: start)
set -euo pipefail

PYTHON=/opt/quantfree/bin/python3
STREAMLIT=/opt/quantfree/bin/streamlit
APP=dashboard/paper-trading/trading_summary.py
PORT=8501
LOG_DIR=logs
LOG_FILE=$LOG_DIR/trading_summary.log

# Always run from the repo root (this script's directory)
cd "$(dirname "$0")"

pid_of_app() {
    pgrep -f "streamlit run $APP" || true
}

is_running() {
    [ -n "$(pid_of_app)" ]
}

start() {
    if is_running; then
        echo "Already running (PID $(pid_of_app | tr '\n' ' ')) on port $PORT"
        return 0
    fi
    mkdir -p "$LOG_DIR"
    setsid nohup "$PYTHON" "$STREAMLIT" run "$APP" \
        --server.port "$PORT" --server.headless true \
        >> "$LOG_FILE" 2>&1 &
    echo "Started (PID $!) -> http://localhost:$PORT  (log: $LOG_FILE)"
}

stop() {
    local pids
    pids=$(pid_of_app)
    if [ -z "$pids" ]; then
        echo "Not running"
        return 0
    fi
    echo "Stopping PID(s) $pids ..."
    kill $pids 2>/dev/null || true
    for _ in $(seq 1 20); do
        is_running || { echo "Stopped"; return 0; }
        sleep 0.5
    done
    echo "Force killing..."
    kill -9 $(pid_of_app) 2>/dev/null || true
    echo "Stopped"
}

status() {
    if is_running; then
        echo "Running (PID $(pid_of_app | tr '\n' ' ')) on port $PORT"
        ss -ltnp 2>/dev/null | grep ":$PORT" || true
    else
        echo "Not running"
        exit 1
    fi
}

case "${1:-start}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; start ;;
    status)  status ;;
    *) echo "Usage: $0 [start|stop|restart|status]" >&2; exit 1 ;;
esac
