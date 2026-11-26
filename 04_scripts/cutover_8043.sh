#!/usr/bin/env bash
# Operator tool. No new release is started by prepare or check.
# Usage: cutover_8043.sh {prepare|check|apply|rollback} NEW RECORD BASELINE_JSON BASELINE_SHA256
# Baseline schema 1 pins units (cwd, source_head, release_manifest_sha256,
# config_sha256, active_state), watch_state, and helper (path, sha256).
set -Eeuo pipefail
umask 077
root=/home/WZQ/citrusscholar
py=/usr/bin/python3.12
scripts=$(dirname -- "$(realpath -e -- "$0")")
unitdir="$HOME/.config/systemd/user"
web=citrusscholar-8043-merged-web.service
worker=citrusscholar-8043-merged-worker.service
intake=citrusscholar-8043-corpus-intake.service
publisher=citrusscholar-8043-corpus-publisher.service
watch=citrusscholar-8043-watchdog.timer
watchjob=citrusscholar-8043-watchdog.service
units=("$web" "$worker" "$intake" "$publisher")
lock="$root/rollbacks/8043-before-auto-corpus-1125/operation.lock"
fail() { printf '%s\n' "$*" >&2; exit 1; }
[[ $# == 5 ]] || fail "Usage: $0 {prepare|check|apply|rollback} NEW RECORD BASELINE_JSON BASELINE_SHA256"
mode=$1 new=$2 record=$3 baseline=$4 baseline_pin=$5
[[ "$mode" == prepare || "$mode" == check || "$mode" == apply || "$mode" == rollback ]] || fail 'Invalid mode'
[[ "$new" =~ ^$root/releases/8043-(reliability|native)-[a-f0-9]{7,40}$ ]] || fail 'Invalid candidate release path'
[[ "$record" =~ ^$root/rollbacks/8043-before-(reliability|native)-[a-f0-9]{7,40}(\.[A-Za-z0-9_-]+)?$ ]] || fail 'Invalid rollback record path'
[[ "$baseline_pin" =~ ^[a-f0-9]{64}$ ]] || fail 'Invalid baseline digest'
[[ $(realpath -e -- "${record%/*}") == "${record%/*}" ]] || fail 'Noncanonical record parent'
[[ $(realpath -e -- "${lock%/*}") == "${lock%/*}" && ! -L "$lock" ]] || fail 'Noncanonical shared operation lock'
if [[ "$mode" == check ]]; then
  if [[ -f "$lock" ]]; then exec 9<"$lock"; flock -ns 9; fi
else
  exec 9<>"$lock"
  flock -n 9
fi

path_for() {
  case "$1" in
    "$web"|"$worker") printf '%s/%s.d/8043-worktree.conf' "$unitdir" "$1" ;;
    *) printf '%s/%s' "$unitdir" "$1" ;;
  esac
}

# Validate file pins without executing anything from the selected release or
# copying currently installed files into an implicitly trusted baseline.
record_data() {
  "$py" -BI "$scripts/cutover_8043_record.py" "$1" "$root" "$new" "$record" "$baseline" "$baseline_pin" "$unitdir" "${units[@]}"
}

context=$(record_data context)
mapfile -t fields <<< "$context"
old=${fields[0]} runtime=${fields[1]} index=${fields[2]} helper=${fields[3]} old_watch=${fields[4]}
declare -A old_cwd old_state
for i in "${!units[@]}"; do
  old_cwd[${units[i]}]=${fields[5+2*i]}
  old_state[${units[i]}]=${fields[6+2*i]}
done
property() { systemctl --user show "$1" -p "$2" --value; }
real_config() {
  [[ -f "$1" && ! -L "$1" && $(realpath -e -- "$1") == "$1" ]]
}
check_running() {
  local mode=$1 u expected
  for u in "${units[@]}"; do
    expected=$new
    [[ "$mode" != old ]] || expected=${old_cwd[$u]}
    [[ $(property "$u" WorkingDirectory) == "$expected" && $(property "$u" ActiveState) == "${old_state[$u]}" ]] || return 1
  done
}
scan_phase() {
  local u p disk live state cwd phase=old have_old=0 have_new=0
  for u in "${units[@]}"; do
    p=$(path_for "$u")
    real_config "$p" || fail "Missing or linked unit config: $u"
    if cmp -s -- "$p" "$record/$u.conf"; then disk=old
    elif cmp -s -- "$p" "$record/staged/$u.conf"; then disk=new
    else fail "Unit config drift is not a recognized cutover phase: $u"; fi
    cwd=$(property "$u" WorkingDirectory)
    case "$cwd" in
      "${old_cwd[$u]}") live=old ;;
      "$new") live=new ;;
      *) fail "Effective WorkingDirectory drift: $u" ;;
    esac
    state=$(property "$u" ActiveState)
    case "$state" in
      active|inactive|failed) ;;
      *) fail "Unstable unit state: $u ($state)" ;;
    esac
    if [[ "$disk" == old || "$live" == old ]]; then have_old=1; fi
    if [[ "$disk" == new || "$live" == new ]]; then have_new=1; fi
    printf '%s disk=%s effective=%s service=%s\n' "$u" "$disk" "$live" "$state" >&2
  done
  if (( have_new && have_old )); then phase=mixed
  elif (( have_new )); then phase=new; fi
  printf '%s\n' "$phase"
}

if [[ "$mode" == prepare ]]; then
  [[ ! -e "$record" && ! -L "$record" ]] || fail 'Record already exists'
  check_running old || fail 'Effective units differ from the pinned baseline'
  [[ $(property "$watch" ActiveState) == "$old_watch" ]] || fail 'Watchdog state differs from the pinned baseline'
  target_record=$record
  record=$(mktemp -d -- "$record.preparing.XXXXXXXX")
  trap 'printf "Incomplete preparation remains private at %s\n" "$record" >&2' EXIT
  record_data capture
  check_running old || fail 'Effective units changed during preparation'
  [[ $(property "$watch" ActiveState) == "$old_watch" ]] || fail 'Watchdog changed during preparation'
  mv -Tn -- "$record" "$target_record"
  [[ ! -e "$record" ]] || fail 'Record appeared during preparation'
  trap - EXIT
  printf 'prepared\n'
  exit
fi

record_data verify
[[ -f "$record/state" && ! -L "$record/state" ]] || fail 'Missing or linked phase state'
state=$(<"$record/state")
case "$state" in
  prepared|stopping|switching|active|rolled-back|rollback-needs-attention) ;;
  *) fail 'Unrecognized recorded activation state' ;;
esac
observed_phase=$(scan_phase)
if [[ "$mode" == check ]]; then
  printf 'record=%s unit_phase=%s\n' "$state" "$observed_phase"
  if cmp -s -- "$index" "$record/active_index.before.json"; then printf 'index=unchanged\n'
  else printf 'index=changed (preserved by rollback)\n'; fi
  if [[ "$observed_phase" == mixed ]]; then printf 'mixed phase is rollback-only\n'; fi
  exit
fi

# The helper byte hash and canonical path were checked before sourcing. It
# provides read-only admission/queue checks, readiness and invocation evidence.
source "$helper"
write_state() {
  local pending
  pending=$(mktemp -- "$record/.state.XXXXXXXX") || return
  printf '%s\n' "$1" > "$pending" || return
  mv -Tf -- "$pending" "$record/state"
}
put_configs() {
  local from=$1 u p tmp
  for u in "${units[@]}"; do
    p=$(path_for "$u") || return
    real_config "$p" || return
    tmp=$(mktemp -- "$p.next.XXXXXXXX") || return
    install -m 0600 -- "$from/$u.conf" "$tmp" || return
    mv -Tf -- "$tmp" "$p" || return
  done
  systemctl --user daemon-reload
}
resume_watch() {
  [[ "$old_watch" != active ]] || systemctl --user start "$watch"
}
start_services() {
  local phase=$1 release=$2 u
  systemctl --user reset-failed "${units[@]}" || true
  systemctl --user start "$worker" || return
  heartbeat "$release" || return
  systemctl --user start "$web" || return
  ready || return
  if [[ "$phase" == new ]]; then cmp -s -- "$index" "$record/active_index.before.json" || return; fi
  for u in "$intake" "$publisher"; do
    if [[ "${old_state[$u]}" == active ]]; then systemctl --user start "$u" || return; fi
  done
  check_running "$phase" || return
  if [[ "$phase" == new ]]; then cmp -s -- "$index" "$record/active_index.before.json" || return; fi
}
restore() {
  systemctl --user stop "$watch" "$watchjob" || return
  systemctl --user stop "${units[@]}" || return
  put_configs "$record" || return
  start_services old "$old" || return
  if ! cmp -s -- "$index" "$record/active_index.before.json"; then
    printf 'Corpus pointer changed. Rollback did not modify the index.\n' >&2
  fi
  resume_watch || return
  write_state rolled-back
}

reconcile_to_old() {
  local web_before watchdog_before
  precheck || return
  watchdog_before=$(property "$watch" ActiveState) || return
  web_before=$(property "$web" ActiveState) || return
  systemctl --user stop "$watch" "$watchjob" || return
  if ! systemctl --user stop "$web" || ! precheck; then
    if [[ "$web_before" == active ]]; then systemctl --user start "$web" || true; fi
    if [[ "$watchdog_before" == active ]]; then systemctl --user start "$watch" || true; fi
    return 1
  fi
  restore
}

if [[ "$mode" == rollback ]]; then
  if [[ "$observed_phase" == old && ( "$state" == prepared || "$state" == rolled-back ) ]] \
      && check_running old && [[ $(property "$watch" ActiveState) == "$old_watch" ]]; then
    printf 'baseline already running\n'
    exit
  fi
  "$py" -BI "$old/launch.py" worker --preflight
  "$py" -BI "$old/launch.py" web --preflight
  if ! reconcile_to_old; then write_state rollback-needs-attention; exit 1; fi
  exit
fi

[[ "$observed_phase" == old && "$state" == prepared ]] || fail 'Apply requires pristine prepared state and old units'
check_running old || fail 'Effective units differ from the pinned baseline'
[[ $(property "$watch" ActiveState) == "$old_watch" ]] || fail 'Watchdog state differs from the pinned baseline'
cmp -s -- "$index" "$record/active_index.before.json" || fail 'Corpus pointer changed after prepare'
manifest_pin=$(record_data candidate)
"$py" -BI "$scripts/stage_8043_release.py" verify --release "$new" --manifest-sha256 "$manifest_pin"
"$py" -BI "$new/launch.py" worker --preflight
"$py" -BI "$new/launch.py" web --preflight
precheck
phase=before-drain
web_was_stopped=false
on_exit() {
  local rc=$?
  trap - EXIT
  case "$phase" in
    before-drain)
      if [[ "$web_was_stopped" == true ]]; then
        systemctl --user start "$web" || { write_state rollback-needs-attention; exit "$rc"; }
      fi
      if resume_watch && check_running old; then write_state prepared
      else write_state rollback-needs-attention; fi ;;
    drained) restore || write_state rollback-needs-attention ;;
    activating) reconcile_to_old || write_state rollback-needs-attention ;;
  esac
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT TERM
write_state stopping
systemctl --user stop "$watch" "$watchjob"
precheck
web_was_stopped=true
systemctl --user stop "$web"
precheck
phase=drained
systemctl --user stop "$worker" "$intake" "$publisher"
cmp -s -- "$index" "$record/active_index.before.json"
write_state switching
put_configs "$record/staged"
phase=activating
start_services new "$new"
"$py" -BI - <<'PY'
from urllib.request import urlopen
base = 'http://127.0.0.1:8043'
for endpoint, marker in (('/', 'ctxViewGrid'), ('/assets/context-view.js', 'CitrusContextView'),
                         ('/assets/citrus-theme.js', 'CitrusTheme')):
    with urlopen(base + endpoint, timeout=5) as response:
        if response.status != 200 or marker.encode() not in response.read():
            raise SystemExit(f'Browser smoke marker missing: {endpoint}')
PY
resume_watch
cmp -s -- "$index" "$record/active_index.before.json"
write_state active
phase=active
