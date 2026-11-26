#!/bin/bash
# Activate a sealed 8043 Web/Worker candidate with an explicit private rollback.
# Usage: activate-1126.sh CANDIDATE_RELEASE RECORD [--check]
# --check validates every precondition and exits without touching any service.
set -Eeuo pipefail
umask 077
r=/home/WZQ/citrusscholar
py=/usr/bin/python3.12
web=citrusscholar-8043-merged-web.service
worker=citrusscholar-8043-merged-worker.service
watch=citrusscholar-8043-watchdog.timer
watchjob=citrusscholar-8043-watchdog.service
new=${1:?candidate release}; record=${2:?rollback record}; mode=${3:-apply}
[[ $new =~ ^$r/releases/8043-(native|reliability)-[a-f0-9]{7,40}$ && $(realpath -e "$new") == "$new" ]]
[[ $record =~ ^$r/rollbacks/8043-before-native-restore-[a-f0-9]{12}\.[A-Za-z0-9]+$ && $(realpath -e "$record") == "$record" ]]
cd "$record"
[[ $(<state) == prepared ]]
sha256sum -c digests > /dev/null
sha256sum -c releases.sha256 > /dev/null
[[ $(sha256sum "$new/release.json" | cut -d' ' -f1) == $(python3 -c 'import json,sys;print(json.load(open("candidate-verified.json"))["manifest_sha256"])') ]]

prop(){ systemctl --user show "$1" -p "$2" --value; }
conf(){ printf '%s/citrusscholar-8043-merged-%s.service.d/8043-worktree.conf' "$HOME/.config/systemd/user" "$1"; }
old=$(prop "$web" WorkingDirectory)
[[ $old == "$r/releases/8043-native-aad34c5a9a25" ]]
runtime=$(python3 -c 'import json;print(json.load(open("release-verified.json"))["runtime_root"])' 2>/dev/null || \
          python3 -c "import json;print(json.load(open('$old/deployment.json'))['runtime_root'])")
[[ -f ./helper.sh ]]
heartbeat(){
  "$py" -BI "$1/source/01_src/scripts/apptainer_launch.py" --config "$1/deployment.json" --role wait-worker
  "$py" -B - "$1" "$worker" <<'PY'
from hashlib import sha256
import json, sqlite3, subprocess, sys, time
from pathlib import Path
r=Path(sys.argv[1]); c=json.loads((r/'deployment.json').read_bytes()); source=Path(c['source_root'])
manifest=(source/'LINUX-SOURCE-MANIFEST.json').read_bytes()
release=sha256(sha256(manifest).digest()+json.dumps(c,sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()
inv=subprocess.check_output(['systemctl','--user','show',sys.argv[2],'-p','InvocationID','--value'],text=True).strip()
assert inv and int(inv,16)
p=Path(c['runtime_root'])/'state/code_executions.sqlite3'
with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as db:
  assert db.execute('SELECT 1 FROM code_worker_release_presence WHERE activation=? AND release=? AND seen>?',
                    (inv,release,time.time()-10)).fetchone(), inv
print('worker heartbeat', release[:12])
PY
}

idle(){
  "$py" - "$runtime/state" <<'PY' || return 1
import sqlite3, sys
from pathlib import Path
root = Path(sys.argv[1])
allowed = {
    'sessions.db': (
        ('task_jobs','status',{'queued','awaiting_input','completed','failed','cancelled'}),
        ('chat_turn_queue','state',{'queued','reconcile_required','awaiting_input','terminal'}),
        ('agent_execution_segments','phase',{'interrupted','terminal'}),
        ('task_questions','status',{'pending','answered','cancelled','expired'}),
        ('task_approvals','status',{'approved','rejected','denied','cancelled','expired'})),
    'code_executions.sqlite3': (
        ('code_executions','status',{'succeeded','failed','cancelled','timed_out'}),
        ('code_execution_waits','state',{'acknowledged','revoked'})),
}
for database, checks in allowed.items():
    with sqlite3.connect((root/database).as_uri()+'?mode=ro',uri=True,timeout=3) as conn:
        for table,column,states in checks:
            found={value:count for value,count in conn.execute(f'SELECT {column},count(*) FROM {table} GROUP BY 1')}
            if set(found)-states: raise SystemExit(f'active or unexpected work in {table}: {found}')
PY
  local jobs
  jobs=$(systemctl --user list-units --type=service --state=active,activating --no-legend --plain 'citrusqa-figure-*.service' 'citrus-code-*.service' 'citrusqa-code-*.service') || return 1
  [[ -z $jobs ]]
}
health(){
  local release=$1 i
  [[ $(prop "$web" WorkingDirectory) == "$release" && $(prop "$worker" WorkingDirectory) == "$release" ]] || return
  for ((i=0;i<120;i++)); do
    [[ $(prop "$web" ActiveState) == failed || $(prop "$worker" ActiveState) == failed || $(prop "$web" NRestarts) -ge 2 ]] && return 1
    if [[ $(prop "$web" ActiveState) == active && $(prop "$worker" ActiveState) == active ]] \
       && curl -fsS --max-time 2 http://127.0.0.1:8043/live > last-live.json 2>/dev/null \
       && curl -fsS --max-time 2 http://127.0.0.1:8043/ready > last-ready.json 2>/dev/null \
       && grep -Eq '"ready"[[:space:]]*:[[:space:]]*true' last-ready.json; then return 0; fi
    sleep 2
  done
  return 1
}
corpus(){
  local role u
  for role in intake publisher; do
    u="citrusscholar-8043-corpus-$role.service"
    printf '%s %s %s %s\n' "$u" "$(prop "$u" WorkingDirectory)" "$(prop "$u" MainPID)" "$(prop "$u" ActiveState)"
  done
}
put(){
  local role=$1 side=$2 target temp
  target=$(conf "$role")
  [[ $(realpath -e "$target") == "$target" && ! -L $target ]]
  cmp -s "$role.before.conf" "$target" || cmp -s "$role.next.conf" "$target" || return 1
  temp=$(mktemp "$target.next.XXXXXXXX")
  install -m 0600 "$role.$side.conf" "$temp"
  mv -Tf "$temp" "$target"
}
served(){
  "$py" -BI - "$new" <<'PY'
from urllib.request import urlopen
from pathlib import Path
from hashlib import sha256
import sys
web = Path(sys.argv[1])/'source/01_src/web'
for label in ('index.html', 'assets/app.js', 'assets/task-state.js', 'assets/task-cards.js',
              'assets/task-transport.js', 'assets/context-view.js', 'assets/workbench/main.js'):
    route = '/' if label == 'index.html' else '/' + label
    source = web / label
    if not source.is_file():
        continue
    with urlopen('http://127.0.0.1:8043' + route, timeout=6) as response:
        served = sha256(response.read()).hexdigest()
    if response.status != 200 or served != sha256(source.read_bytes()).hexdigest():
        raise SystemExit('served bytes mismatch ' + route)
print('sealed source bytes match')
PY
}

# ---- preconditions ------------------------------------------------------
[[ $(prop "$web" ActiveState) == active && $(prop "$worker" ActiveState) == active ]]
[[ $(prop "$watch" ActiveState) == active ]]
for role in web worker; do cmp -s "$role.before.conf" "$(conf $role)" || { echo "unit config drift: $role" >&2; exit 1; }; done
[[ $(prop "$web" WorkingDirectory) == "$old" && $(prop "$worker" WorkingDirectory) == "$old" ]]
idle || { echo "not quiescent; unresolved work remains" >&2; exit 1; }
corpus > corpus-units.before
if [[ $mode == --check ]]; then echo "CHECK_OK old=$old new=$new"; exit 0; fi

# ---- activation ---------------------------------------------------------
lock="$r/rollbacks/8043-before-auto-corpus-1125/operation.lock"
[[ -f $lock && ! -L $lock ]]; exec 9<>"$lock"; flock -n 9
exec > >(tee -a cutover.log) 2>&1
sha256sum -c index.sha256 > /dev/null
phase=before-drain; web_stopped=false
restore(){
  local rc=$?
  trap - EXIT INT TERM
  ((rc==0)) && return
  echo "activation failed rc=$rc phase=$phase; restoring $old"
  if [[ $phase == before-drain ]]; then
    [[ $web_stopped == true ]] && { systemctl --user start "$web" || { echo rolled-back-needs-attention > state; exit "$rc"; }; }
  else
    systemctl --user stop "$web" || :
    idle || { echo rollback-needs-attention > state; systemctl --user start "$watch" || :; exit "$rc"; }
    systemctl --user stop "$worker" || :
    put web before && put worker before && systemctl --user daemon-reload
    systemctl --user reset-failed "$web" "$worker" || :
    systemctl --user start "$worker" && heartbeat "$old" && systemctl --user start "$web" && health "$old" \
      || { echo rollback-needs-attention > state; exit "$rc"; }
  fi
  systemctl --user start "$watch" || { echo rollback-needs-attention > state; exit "$rc"; }
  echo rolled-back > state
  exit "$rc"
}
trap restore EXIT
trap 'exit 130' INT TERM
echo stopping > state
systemctl --user stop "$watch" "$watchjob"
idle
web_stopped=true
systemctl --user stop "$web"
idle
phase=drained
systemctl --user stop "$worker"
echo switching > state
put web next
put worker next
systemctl --user daemon-reload
systemctl --user reset-failed "$web" "$worker" || :
systemctl --user start "$worker"
heartbeat "$new"
echo starting-web > state
systemctl --user start "$web"
health "$new"
served
sha256sum -c index.sha256 > /dev/null
corpus > corpus-units.after
cmp corpus-units.before corpus-units.after
systemctl --user start "$watch"
health "$new"
echo active > state
trap - EXIT INT TERM
echo "ACTIVATED $new"
