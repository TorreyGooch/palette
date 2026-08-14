#!/usr/bin/env bash
# Start, stop and inspect the corpus API on this machine.
#
# Deliberately on demand rather than a service: this box shares its memory
# and GPU with generation work, and an idle API holding a 1.3 GB embedding
# model is memory taken from that for nothing. The desktop app drives this
# over ssh, and you can run it by hand.
#
#   ./server-app.sh start|stop|restart|status
#
# status prints JSON, because the app parses it.

set -eo pipefail

REPO="${PALETTE_REPO:-$HOME/palette}"
PY="${PALETTE_PYTHON:-$HOME/miniconda3/envs/palette/bin/python}"
ENVFILE="${PALETTE_ENV:-$HOME/.palette-env}"
SESSION="${PALETTE_TMUX:-app}"
PORT="${PALETTE_PORT:-7862}"
LOG="${PALETTE_LOG:-$HOME/palette-app.log}"

listening() { ss -tln 2>/dev/null | grep -q ":$PORT "; }

app_pid() {
  pgrep -f "run.py" 2>/dev/null | while read -r pid; do
    if tr '\0' ' ' < "/proc/$pid/environ" 2>/dev/null | grep -q "PALETTE_PORT=$PORT"; then
      echo "$pid"; return
    fi
  done | head -1
}

start_app() {
  if listening; then echo "already running on $PORT" >&2; return 0; fi
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  tmux new-session -d -s "$SESSION" \
    "source '$ENVFILE' 2>/dev/null; cd '$REPO' && PALETTE_API_ONLY=1 \
     PALETTE_HOST=tailscale PALETTE_PORT=$PORT '$PY' run.py > '$LOG' 2>&1"
  for _ in $(seq 1 40); do
    listening && { echo "started on $PORT" >&2; return 0; }
    sleep 0.5
  done
  echo "did not come up within 20s; see $LOG" >&2
  return 1
}

stop_app() {
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  pid=$(app_pid || true)
  [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  for _ in $(seq 1 20); do
    listening || { echo "stopped" >&2; return 0; }
    sleep 0.5
  done
  echo "still listening on $PORT" >&2
  return 1
}

status_json() {
  local running=false rss_mb=0 pid="" version="" uptime=""
  if listening; then running=true; fi
  pid=$(app_pid || true)
  if [ -n "$pid" ] && [ -r "/proc/$pid/status" ]; then
    rss_mb=$(awk '/VmRSS/ {printf "%.0f", $2/1024}' "/proc/$pid/status")
    uptime=$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')
  fi
  version=$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)

  local gpu_used=0 gpu_total=0 gpu_util=0
  if command -v nvidia-smi >/dev/null 2>&1; then
    read -r gpu_used gpu_total gpu_util < <(
      nvidia-smi --query-gpu=memory.used,memory.total,utilization.gpu \
                 --format=csv,noheader,nounits 2>/dev/null | tr -d ',' | head -1)
  fi

  local mem_avail_mb
  mem_avail_mb=$(awk '/MemAvailable/ {printf "%.0f", $2/1024}' /proc/meminfo)

  cat <<EOF
{"running": $running,
 "port": $PORT,
 "pid": "${pid}",
 "uptime": "${uptime}",
 "version": "${version}",
 "rss_mb": ${rss_mb:-0},
 "mem_available_mb": ${mem_avail_mb:-0},
 "gpu_used_mb": ${gpu_used:-0},
 "gpu_total_mb": ${gpu_total:-0},
 "gpu_util": ${gpu_util:-0}}
EOF
}

case "${1:-status}" in
  start)   start_app;   status_json ;;
  stop)    stop_app;    status_json ;;
  restart) stop_app || true; start_app; status_json ;;
  status)  status_json ;;
  *) echo "usage: $0 start|stop|restart|status" >&2; exit 2 ;;
esac
