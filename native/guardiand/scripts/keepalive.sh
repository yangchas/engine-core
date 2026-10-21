#!/usr/bin/env bash
# Versioned cron wrapper: keep Redis guard/supervisor alive and recover a stalled daemon.
set -u
D=${GUARDIAN_HOME:-/home/exedev/core-data/guardian-native}
STATE_DIR=${GUARDIAN_STATE_DIR:-$D/state}
HEARTBEAT=${GUARDIAN_HEARTBEAT:-$STATE_DIR/heartbeat.json}
PID_FILE=${GUARDIAN_PID_FILE:-$STATE_DIR/guardiand.pid}
LOG=${GUARDIAN_LOG:-$D/keepalive.log}
LOCK=${GUARDIAN_LOCK:-$D/keepalive.lock}
RESTARTS=${GUARDIAN_RESTARTS_FILE:-$STATE_DIR/keepalive-restarts}
STARTED=${GUARDIAN_GRACE_FILE:-$STATE_DIR/keepalive-started}
SUPERVISOR=${GUARDIAN_SUPERVISOR:-$D/supervisor.sh}
MAX_AGE=${GUARDIAN_MAX_AGE:-120}
GRACE=${GUARDIAN_START_GRACE:-90}
CONFIRM=${GUARDIAN_CONFIRM_SECONDS:-2}
TERM_WAIT=${GUARDIAN_TERM_SECONDS:-10}
now() { printf '%s' "${GUARDIAN_NOW:-$(date +%s)}"; }
log() { printf '%s %s\n' "$(date '+%F %T')" "$*" >> "$LOG"; }
proc_identity() {
  GUARDIAN_PROC_ROOT=${GUARDIAN_PROC_ROOT:-/proc} python3 - "$1" <<'PY'
import os
import sys
try:
    raw = open(f"{os.environ['GUARDIAN_PROC_ROOT']}/{sys.argv[1]}/stat").read().strip()
    left, right = raw.rsplit(")", 1)
    comm = left[left.index("(") + 1:]
    fields = right.split()
    print(comm, fields[19]) # field 22 (starttime), after pid/comm fields 1-2
except Exception:
    print(" ")
PY
}

check_stall() {
  local pid hb1 hb2 tick1 tick2 age started current recent count identity comm starttime current_identity
  pid=${GUARDIAN_TEST_PID:-$(cat "$PID_FILE" 2>/dev/null || true)}
  if [[ -z "$pid" ]]; then pid=$(pgrep -x guardiand 2>/dev/null | head -n 1 || true); fi
  [[ "$pid" =~ ^[0-9]+$ ]] || return 0
  kill -0 "$pid" 2>/dev/null || return 0
  identity=$(proc_identity "$pid")
  read -r comm starttime <<<"$identity"
  [[ "$comm" == guardiand && "$starttime" =~ ^[0-9]+$ ]] || return 0
  [[ -r "$HEARTBEAT" ]] || return 0
  hb1=$(python3 - "$HEARTBEAT" <<'PY'
import json,sys
try:
 d=json.load(open(sys.argv[1])); print(d.get('last_tick_at',''), d.get('tick_seq',''), d.get('boot_id',''))
except Exception: print('  ')
PY
)
  read -r tick1_seq tick1_at tick1_boot <<<"$(awk '{print $2, $1, $3}' <<<"$hb1")"
  [[ "$tick1_at" && "$tick1_seq" ]] || return 0
  current=$(now)
  age=$(python3 - "$tick1_at" "$current" <<'PY'
from datetime import datetime
import sys
v=sys.argv[1]
try:
 t=datetime.fromisoformat(v.replace('Z','+00:00')).timestamp()
 print(max(0,int(float(sys.argv[2])-t)))
except Exception: print(-1)
PY
)
  [[ "$age" =~ ^[0-9]+$ ]] && (( age > MAX_AGE )) || return 0
  # A new process boot_id starts a persisted 90s grace window.
  local marker
  marker=$(sed -n '1p' "$STARTED" 2>/dev/null || true)
  started=$(sed -n '2p' "$STARTED" 2>/dev/null || true)
  if [[ "$marker" != "$pid:$tick1_boot" || ! "$started" =~ ^[0-9]+$ ]]; then
    printf '%s\n%s\n' "$pid:$tick1_boot" "$current" > "$STARTED"
    log "observed daemon start pid=$pid boot_id=$tick1_boot; startup grace begins"
    return 0
  fi
  (( current - started >= GRACE )) || return 0
  sleep "$CONFIRM"
  hb2=$(python3 - "$HEARTBEAT" <<'PY'
import json,sys
try:
 d=json.load(open(sys.argv[1])); print(d.get('last_tick_at',''), d.get('tick_seq',''))
except Exception: print(' ')
PY
)
  read -r tick2_at tick2_seq <<<"$hb2"
  [[ "$tick1_seq" == "$tick2_seq" && "$tick1_at" == "$tick2_at" ]] || return 0
  recent=$(awk -v n="$current" '$1 >= n-3600 {print $1}' "$RESTARTS" 2>/dev/null || true)
  if [[ -n "$recent" ]]; then count=$(wc -l <<<"$recent" | tr -d ' '); else count=0; fi
  if (( count >= 3 )); then
    log "stalled pid=$pid tick_seq=$tick1_seq age=${age}s restart limit reached; alert command invoked"
    if [[ -n "${GUARDIAN_ALERT_COMMAND:-}" ]]; then "$GUARDIAN_ALERT_COMMAND" "guardiand stalled; restart limit reached" >> "$LOG" 2>&1 || true; fi
    return 0
  fi
  current_identity=$(proc_identity "$pid")
  [[ "$current_identity" == "$identity" ]] || { log "pid=$pid identity changed before SIGTERM; skip stale recovery"; return 0; }
  log "stalled pid=$pid tick_seq=$tick1_seq age=${age}s; sending SIGTERM"
  printf '%s\n' "$current" >> "$RESTARTS"
  kill -TERM "$pid" 2>/dev/null || return 0
  sleep "$TERM_WAIT"
  if kill -0 "$pid" 2>/dev/null; then
    current_identity=$(proc_identity "$pid")
    [[ "$current_identity" == "$identity" ]] || { log "pid=$pid identity changed before SIGKILL; skip stale recovery"; return 0; }
    log "pid=$pid remained after ${TERM_WAIT}s; sending SIGKILL"
    kill -KILL "$pid" 2>/dev/null || true
  fi
}

if [[ "${1:-}" == check ]]; then check_stall; exit 0; fi
mkdir -p "$(dirname "$HEARTBEAT")"
(printf 'PING\r\n'; sleep 0.3) | timeout 2 nc 127.0.0.1 6380 2>/dev/null | grep -q PONG || (cd /home/exedev/fw/guard && nohup python3 redis_guard.py >/dev/null 2>&1 &)
check_stall
exec flock -n "$LOCK" "$SUPERVISOR"
