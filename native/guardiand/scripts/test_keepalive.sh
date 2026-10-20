#!/usr/bin/env bash
set -euo pipefail
SCRIPT="$(cd "$(dirname "$0")" && pwd)/keepalive.sh"
TMP=$(mktemp -d)
trap 'kill "${fake_pid:-}" 2>/dev/null || true' EXIT
mkdir -p "$TMP/state"
start_fake() { bash -c 'exec -a guardiand sleep 300' & fake_pid=$!; }
heartbeat() { printf '{"last_tick_at":"%s","tick_seq":%s}\n' "$1" "$2" > "$TMP/state/heartbeat.json"; }
run_case() { env GUARDIAN_STATE_DIR="$TMP/state" GUARDIAN_PID_FILE="$TMP/pid" GUARDIAN_LOG="$TMP/log" GUARDIAN_LOCK="$TMP/lock" GUARDIAN_SUPERVISOR="$TMP/no-supervisor" GUARDIAN_RESTARTS_FILE="$TMP/restarts" GUARDIAN_GRACE_FILE="$TMP/started" GUARDIAN_MAX_AGE=120 GUARDIAN_CONFIRM_SECONDS=0 GUARDIAN_TERM_SECONDS=0 GUARDIAN_NOW="$2" GUARDIAN_TEST_PID="$fake_pid" bash "$SCRIPT" check; }

# Fresh heartbeat must leave the process untouched.
start_fake; echo "$fake_pid" > "$TMP/pid"; heartbeat "$(date -u +%Y-%m-%dT%H:%M:%SZ)" 1
run_case unused "$(date +%s)"
kill -0 "$fake_pid"
kill "$fake_pid"; wait "$fake_pid" 2>/dev/null || true; unset fake_pid

# Stalled heartbeat outside startup grace terminates the exact PID.
start_fake; echo "$fake_pid" > "$TMP/pid"; heartbeat 2000-01-01T00:00:00Z 2; printf '%s:%s\n1\n' "$fake_pid" "" > "$TMP/started"
run_case unused "$(date +%s)"
if kill -0 "$fake_pid" 2>/dev/null; then echo 'FAIL: stalled process survived' >&2; exit 1; fi
wait "$fake_pid" 2>/dev/null || true; unset fake_pid

# Startup grace suppresses a stale heartbeat.
start_fake; echo "$fake_pid" > "$TMP/pid"; heartbeat 2000-01-01T00:00:00Z 3; printf '%s:%s\n%s\n' "$fake_pid" "" "$(date +%s)" > "$TMP/started"
run_case unused "$(date +%s)"
kill -0 "$fake_pid"
kill "$fake_pid"; wait "$fake_pid" 2>/dev/null || true; unset fake_pid

# Three restarts in the rolling hour suppress another kill.
start_fake; echo "$fake_pid" > "$TMP/pid"; heartbeat 2000-01-01T00:00:00Z 4; printf '%s:%s\n1\n' "$fake_pid" "" > "$TMP/started"
printf '%s\n%s\n%s\n' "$(( $(date +%s) - 20 ))" "$(( $(date +%s) - 10 ))" "$(date +%s)" > "$TMP/restarts"
run_case unused "$(date +%s)"
kill -0 "$fake_pid"
echo 'PASS: fresh, stalled, grace, restart-limit'
