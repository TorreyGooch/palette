#!/usr/bin/env bash
# Start, stop and inspect the corpus API on this machine.
#
# Deliberately on demand rather than a service: this box shares its memory
# and GPU with generation work, and an idle API holding a 1.3 GB embedding
# model is memory taken from that for nothing. The desktop app drives this
# over ssh, and you can run it by hand.
#
#   ./server-app.sh start|stop|restart|update|status
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

# uvicorn binds one address, so serving loopback as well as the tailnet
# would mean 0.0.0.0 - which also exposes the LAN, on an app with no
# authentication. Publishing the resolved address instead costs nothing and
# removes the actual hazard: local scripts hardcoding an IP that changes if
# the node is ever re-added to the tailnet.
#   API=$(cat ~/.palette-api-url)
URLFILE="${PALETTE_URL_FILE:-$HOME/.palette-api-url}"

publish_url() {
  local ip
  ip=$(tailscale ip -4 2>/dev/null | head -1)
  [ -z "$ip" ] && ip="127.0.0.1"
  printf 'http://%s:%s\n' "$ip" "$PORT" > "$URLFILE"
}

start_app() {
  if listening; then publish_url; echo "already running on $PORT" >&2; return 0; fi
  tmux kill-session -t "$SESSION" 2>/dev/null || true
  tmux new-session -d -s "$SESSION" \
    "source '$ENVFILE' 2>/dev/null; cd '$REPO' && PALETTE_API_ONLY=1 \
     PALETTE_HOST=tailscale PALETTE_PORT=$PORT '$PY' run.py > '$LOG' 2>&1"
  for _ in $(seq 1 40); do
    listening && { publish_url; echo "started on $PORT ($(cat "$URLFILE"))" >&2; return 0; }
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

# Bring the checkout forward, and restart only what was already running.
#
# The desktop's deploy.ps1 pushes and then pulls here over ssh, which needs
# that machine switched on and someone to run it. This is the same step from
# this side, for when it is not.
#
# It never *starts* a stopped app. This box is on demand precisely so it is
# not holding an embedding model for nobody, and updating the code is not a
# request to serve it.
update_app() {
  cd "$REPO"

  # Same commit, different code is the failure this guards against - a mode
  # bit, an edit made here to get something working. A fast-forward over that
  # either refuses or carries it silently onward, and HEAD says nothing.
  local dirty
  dirty=$(git status --porcelain)
  if [ -n "$dirty" ]; then
    echo "tree is dirty - refusing to update:" >&2
    echo "$dirty" >&2
    return 1
  fi

  local before after upstream
  before=$(git rev-parse --short HEAD)
  upstream=$(git rev-parse --abbrev-ref '@{u}' 2>/dev/null || echo "")
  if [ -z "$upstream" ]; then
    echo "no upstream branch to update from" >&2
    return 1
  fi

  git fetch --quiet origin || { echo "could not reach origin" >&2; return 1; }
  git merge --ff-only --quiet "@{u}" || {
    echo "not a fast-forward: this checkout has diverged from $upstream" >&2
    return 1
  }
  after=$(git rev-parse --short HEAD)

  if [ "$before" = "$after" ]; then
    echo "already on $after" >&2
  else
    echo "updated $before -> $after" >&2
  fi

  # Nothing here runs the suite: this environment has no pytest, and the
  # tests ran before the push (hooks/pre-push) and again in CI. If either of
  # those stops being true, this is where the check belongs.

  # What gets restarted is decided by what the app is *serving*, not by
  # whether HEAD moved. Those are different questions: someone who pulled by
  # hand and did not restart leaves a process on the old module, and this
  # would otherwise answer "already on X" and walk past exactly the state
  # `stale` exists to report. The promise is that the box is current
  # afterwards, so ask the running app rather than the repo.
  if ! listening; then
    echo "not running - nothing to restart" >&2
    return 0
  fi

  # `|| true` is load-bearing under `set -eo pipefail`: running_version is a
  # curl piped into python, and a failing pipeline in a command substitution
  # would abort the script *after* the pull - leaving exactly the stale app
  # this function exists to prevent, and saying nothing about it. Seen for
  # real when the box was under memory pressure and curl timed out.
  local serving
  serving=$(running_version || true)
  if [ -n "$serving" ] && [ "$serving" = "$after" ]; then
    echo "app already serving $after" >&2
    return 0
  fi

  echo "app is serving ${serving:-an unknown build} - restarting onto $after" >&2
  stop_app || true
  start_app
}

# The version a process is serving is fixed when it imports, so a pull does
# not change it and only a restart does. Ask the API itself rather than the
# repo: reporting HEAD here once claimed the app was up to date while it had
# been serving a commit from the previous day for thirty-three hours.
running_version() {
  local url
  url=$(cat "$URLFILE" 2>/dev/null || echo "http://127.0.0.1:$PORT")
  curl -s --max-time 5 "$url/api/qs/status" 2>/dev/null \
    | "$PY" -c 'import json,sys
try: print(json.load(sys.stdin)["palette"]["version"])
except Exception: print("")' 2>/dev/null
}

status_json() {
  local running=false rss_mb=0 pid="" version="" repo_version="" uptime="" stale=false
  if listening; then running=true; fi
  pid=$(app_pid || true)
  if [ -n "$pid" ] && [ -r "/proc/$pid/status" ]; then
    rss_mb=$(awk '/VmRSS/ {printf "%.0f", $2/1024}' "/proc/$pid/status")
    uptime=$(ps -o etime= -p "$pid" 2>/dev/null | tr -d ' ')
  fi
  repo_version=$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)
  if [ "$running" = true ]; then
    version=$(running_version)
  fi
  [ -z "$version" ] && version="$repo_version"
  if [ "$running" = true ] && [ "$version" != "$repo_version" ]; then
    stale=true
  fi

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
 "repo_version": "${repo_version}",
 "stale": ${stale},
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
  update)  update_app;  status_json ;;
  status)  status_json ;;
  *) echo "usage: $0 start|stop|restart|update|status" >&2; exit 2 ;;
esac
