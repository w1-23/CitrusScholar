#!/usr/bin/env bash
# Operator-owned Linux activation. The gate receipt is created only after isolated acceptance.
set -Eeuo pipefail
umask 077
root=/home/WZQ/citrusscholar
old="$root/releases/8043-external-skills-2234d15"
py=/usr/bin/python3.12
previous_tools="$root/rollbacks/8043-before-external-skills-1125"
unitdir="$HOME/.config/systemd/user"
web=citrusscholar-8043-merged-web.service
worker=citrusscholar-8043-merged-worker.service
intake=citrusscholar-8043-corpus-intake.service
publisher=citrusscholar-8043-corpus-publisher.service
watch=citrusscholar-8043-watchdog.timer
watchjob=citrusscholar-8043-watchdog.service
units=("$web" "$worker" "$intake" "$publisher")
mode=${1:-}; new=${2:-}; record=${3:-}
test $# = 3
[[ "$mode" == prepare || "$mode" == apply || "$mode" == rollback ]]
[[ "$new" =~ ^/home/WZQ/citrusscholar/releases/8043-reliability-[a-f0-9]{7,40}$ ]]
[[ "$record" =~ ^/home/WZQ/citrusscholar/rollbacks/8043-before-reliability-[a-f0-9]{7,40}$ ]]
test "$(realpath -e -- "$new")" = "$new"
test "$(realpath -e -- "$old")" = "$old"
source "$previous_tools/helper.sh"
runtime=$($py -c 'import json,sys; print(json.load(open(sys.argv[1]))["runtime_root"])' "$old/deployment.json")
index="$root/runtime/8043-isolated-corpus/active_index.json"
exec 9>"$root/rollbacks/8043-before-auto-corpus-1125/operation.lock"
flock -n 9
path_for() {
  case "$1" in "$web"|"$worker") printf '%s/%s.d/8043-worktree.conf' "$unitdir" "$1";; *) printf '%s/%s' "$unitdir" "$1";; esac
}
check_units() {
  local release=$1 u
  for u in "${units[@]}"; do
    systemctl --user is-active --quiet "$u" || return
    test "$(systemctl --user show "$u" -p WorkingDirectory --value)" = "$release" || return
  done
}
put_configs() {
  local from=$1 u p
  for u in "${units[@]}"; do
    p=$(path_for "$u") || return
    install -m 0600 "$from/$u.conf" "$p.next" || return
    mv -Tf -- "$p.next" "$p" || return
  done
  systemctl --user daemon-reload
}
check_config() {
  local from=$1 u
  for u in "${units[@]}"; do cmp -s -- "$(path_for "$u")" "$from/$u.conf" || return; done
}
resume_watch() { if [[ "$(cat "$record/watch.state")" == active ]]; then systemctl --user start "$watch"; fi; }
start_services() {
  local release=$1 require_unchanged=${2:-true}
  systemctl --user reset-failed "${units[@]}" || true
  systemctl --user start "$worker" || return
  heartbeat "$release" || return
  systemctl --user start "$web" || return
  ready || return
  if [[ "$require_unchanged" == true ]]; then cmp -s "$index" "$record/active_index.before.json" || return; fi
  systemctl --user start "$intake" "$publisher" || return
  check_units "$release" || return
  if [[ "$require_unchanged" == true ]]; then cmp -s "$index" "$record/active_index.before.json" || return; fi
}
restore() {
  systemctl --user stop "$watch" "$watchjob" || true
  systemctl --user stop "${units[@]}" || return
  put_configs "$record" || return
  start_services "$old" false || return
  if ! cmp -s "$index" "$record/active_index.before.json"; then
    printf 'Corpus pointer differs from the pre-cutover observation; no index bytes were changed by rollback.\n' >&2
  fi
  resume_watch || return
  printf 'rolled-back\n' > "$record/state"
}
if [[ "$mode" == prepare ]]; then
  "$py" - "$old/deployment.json" "$new/deployment.json" <<'PY'
import json,re,sys
from pathlib import Path
old,new=(json.loads(Path(p).read_bytes()) for p in sys.argv[1:]); assert new['port']==old['port']==8043
assert new['listen_host']==old['listen_host']=='127.0.0.1' and new['external_fusion_required'] is True
for key in ('runtime_root','corpus_root','code_staging','figure_staging','isolated_corpus','gpu_device','model_origin','model_config_root'):
    assert new[key]==old[key],key
for item in old['historical_skill_releases']:
    assert item in new['historical_skill_releases']
assert any(i['mount_path']==old['skill_release'] and i['sha256']==old['skill_release_sha256'] for i in new['historical_skill_releases'])
PY
  check_units "$old"
  mkdir -- "$record"
  mkdir -- "$record/staged"
  cp -- "$index" "$record/active_index.before.json"
  systemctl --user show "$watch" -p ActiveState --value > "$record/watch.state"
  for u in "${units[@]}"; do
    p=$(path_for "$u")
    test -f "$p" && test ! -L "$p"
    install -m 0600 "$p" "$record/$u.conf"
    "$py" - "$record/$u.conf" "$record/staged/$u.conf" "$old" "$new" "$u" <<'PY'
from pathlib import Path
import json,sys
original,out,old,new,unit=sys.argv[1:]; source=Path(original).read_text()
assert f'WorkingDirectory={old}' in source and f'{old}/' in source
result=source.replace(old,new)
if unit.endswith(('corpus-intake.service','corpus-publisher.service')):
    before=json.loads((Path(old)/'deployment.json').read_bytes())['app_sif']
    after=json.loads((Path(new)/'deployment.json').read_bytes())['app_sif']
    assert source.count(before)==1
    result=result.replace(before,after)
Path(out).write_text(result)
PY
  done
  "$py" - "$record" "$old" "$new" <<'PY'
import json,sys
from pathlib import Path
record,old,new=map(Path,sys.argv[1:]); commit=json.loads((new/'deployment.json').read_bytes())['source_head']
(record/'identity.json').write_text(json.dumps({'old':str(old),'new':str(new),'source_head':commit},sort_keys=True)+'\n')
PY
  (cd -- "$record" && sha256sum identity.json active_index.before.json watch.state ./*.conf staged/*.conf > SHA256SUMS)
  printf 'prepared\n' > "$record/state"
  exit
fi
test "$(realpath -e -- "$record")" = "$record"
(cd -- "$record" && sha256sum -c SHA256SUMS)
"$py" - "$record" "$old" "$new" <<'PY'
import json,sys
from pathlib import Path
record,old,new=map(Path,sys.argv[1:]); config=json.loads((new/'deployment.json').read_bytes())
assert json.loads((record/'identity.json').read_bytes())=={'old':str(old),'new':str(new),'source_head':config['source_head']}
for p in (record/'staged').glob('*.conf'):
    text=p.read_text();assert f'WorkingDirectory={new}\n' in text and str(old) not in text
PY
if [[ "$mode" == rollback ]]; then
  check_config "$record/staged"
  precheck
  web_before=$(systemctl --user show "$web" -p ActiveState --value)
  systemctl --user stop "$web"
  if ! precheck; then
    if [[ "$web_before" == active ]]; then systemctl --user start "$web" || true; fi
    exit 1
  fi
  restore
  exit
fi
check_units "$old"
check_config "$record"
cmp -s "$index" "$record/active_index.before.json"
manifest_pin=$("$py" - "$record/isolated-gate.json" "$new/deployment.json" <<'PY'
from pathlib import Path
from hashlib import sha256
import json,re,sys
r=json.loads(Path(sys.argv[1]).read_bytes()); p=Path(sys.argv[2]); d=p.read_bytes(); c=json.loads(d)
assert r['deployment_sha256']==sha256(d).hexdigest() and r['source_head']==c['source_head']
sha=r['manifest_sha256']; assert re.fullmatch('[0-9a-f]{64}',sha)
assert r['isolated_port'] not in {8043,8145} and r['isolated_runtime_root']!=c['runtime_root']
for key in ('app_sha256','sandbox_sha256','namespace_sha256','skill_release_sha256'):
    assert r[key]==c[key],key
assert all(r['passed'][k] is True for k in ('pinned_worker','app_readiness','browser','audit_delivery','r_artifacts','full_regression','source_architecture'))
print(sha)
PY
)
"$py" -BI "$new/source/04_scripts/stage_8043_release.py" verify --release "$new" --manifest-sha256 "$manifest_pin"
"$py" -BI "$new/launch.py" worker --preflight
"$py" -BI "$new/launch.py" web --preflight
precheck
phase=stopping-before-swap
on_exit() {
  local rc=$?
  case "$phase" in
    swapped) restore || printf 'rollback-needs-attention\n' > "$record/state" ;;
    stopping-before-swap) start_services "$old" false || true; resume_watch || true ;;
  esac
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT TERM
systemctl --user stop "$watch" "$watchjob"
precheck
# Removing loopback admission first closes the race between checking and draining.
systemctl --user stop "$web"
precheck
systemctl --user stop "$worker" "$intake" "$publisher"
cmp -s "$index" "$record/active_index.before.json"
phase=swapped
printf 'switching\n' > "$record/state"
put_configs "$record/staged"
start_services "$new"
"$py" - "$new/deployment.json" <<'PY'
import json,sys
from urllib.request import urlopen
from pathlib import Path
path=Path(sys.argv[1]); c=json.loads(path.read_bytes()); base='http://127.0.0.1:'+str(c['port'])
for endpoint, marker in (('/','ctxViewGrid'),('/assets/context-view.js','CitrusContextView'),('/assets/citrus-theme.js','CitrusTheme')):
    with urlopen(base+endpoint,timeout=5) as r:
        assert r.status==200 and marker.encode() in r.read(),endpoint
PY
resume_watch
cmp -s "$index" "$record/active_index.before.json"
printf 'active\n' > "$record/state"
phase=active
