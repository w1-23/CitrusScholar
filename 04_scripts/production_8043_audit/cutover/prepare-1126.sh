#!/bin/bash
# Prepare a private 8043 Web/Worker rollout record for a sealed candidate.
# Usage: prepare_record.sh CANDIDATE_RELEASE
# Writes only into a new private rollbacks/ record; starts nothing.
set -Eeuo pipefail
umask 077
r=/home/WZQ/citrusscholar
py=/usr/bin/python3.12
unitdir=$HOME/.config/systemd/user
web=citrusscholar-8043-merged-web.service
worker=citrusscholar-8043-merged-worker.service
watch=citrusscholar-8043-watchdog.timer
new=${1:?candidate release}
[[ $new =~ ^$r/releases/8043-(native|reliability)-[a-f0-9]{7,40}$ ]] || { echo "invalid candidate" >&2; exit 2; }
[[ $(realpath -e "$new") == "$new" && -f $new/release.json ]] || { echo "unsealed candidate" >&2; exit 2; }
prop(){ systemctl --user show "$1" -p "$2" --value; }
old=$(prop "$web" WorkingDirectory)
[[ $old =~ ^$r/releases/8043-(native|reliability)-[a-f0-9]{7,40}$ && $old != "$new" ]]
[[ $(prop "$worker" WorkingDirectory) == "$old" ]]
[[ $(prop "$web" ActiveState) == active && $(prop "$worker" ActiveState) == active ]]

suffix=$(printf '%04x%04x' $RANDOM $RANDOM)
record=$r/rollbacks/8043-before-native-restore-$(basename "$new" | sed 's/^8043-native-//').$suffix
mkdir "$record"; cd "$record"

cp -a "$unitdir/$web.d/8043-worktree.conf" web.before.conf
cp -a "$unitdir/$worker.d/8043-worktree.conf" worker.before.conf
for u in "$web" "$worker"; do
  printf '%s\n' "Id=$u" "WorkingDirectory=$(prop "$u" WorkingDirectory)" "ActiveState=$(prop "$u" ActiveState)" \
    "MainPID=$(prop "$u" MainPID)" "InvocationID=$(prop "$u" InvocationID)" "NRestarts=$(prop "$u" NRestarts)"
done > units.txt
printf 'watch_state=%s\n' "$(prop "$watch" ActiveState)" >> units.txt
sha256sum "$old/release.json" "$old/deployment.json" "$new/release.json" "$new/deployment.json" > releases.sha256
sha256sum "$r/runtime/8043-isolated-corpus/active_index.json" > index.sha256
cp -a "$r/rollbacks/8043-before-native-restore-62162e99.4d934bce/helper.sh" helper.sh

# Candidate facts are copied from the sealed manifest, never re-derived from a live process.
"$py" -BI - "$new" "$old" > candidate-verified.json <<'PY'
import hashlib, json, sys
from pathlib import Path
new, old = Path(sys.argv[1]), Path(sys.argv[2])
m = json.loads((new/'release.json').read_bytes()); d = json.loads((new/'deployment.json').read_bytes())
o = json.loads((old/'deployment.json').read_bytes())
assert hashlib.sha256((new/'deployment.json').read_bytes()).hexdigest() == m['artifacts']['deployment.json']
assert hashlib.sha256((new/'launch.py').read_bytes()).hexdigest() == m['artifacts']['launch.py']
assert m['source_commit'] == d['source_head'] and len(m['source_commit']) == 40
assert m['baseline']['deployment'] == str(old/'deployment.json')
assert d['port'] == 8043 and d['listen_host'] == '127.0.0.1'
for key in ('runtime_root','corpus_root','code_staging','figure_staging','isolated_corpus',
            'gpu_device','model_origin','model_config_root','external_fusion_required'):
    assert d[key] == o[key], key
print(json.dumps({'release': str(new), 'source_commit': m['source_commit'],
  'source_files': len(json.loads((new/'source-sha256.json').read_bytes())),
  'manifest_sha256': hashlib.sha256((new/'release.json').read_bytes()).hexdigest(),
  'skill_release_sha256': m['active_skill_sha256'], 'images': m['images']}, sort_keys=True))
PY
# staged unit configs: only the two release paths change.
for role in web worker; do
  "$py" -BI - "$role.before.conf" "$role.next.conf" "$old" "$new" <<'PY'
import re, sys
from pathlib import Path
src, dst, old, new = sys.argv[1:5]
text = Path(src).read_text(encoding='utf-8')
assert re.findall(r'(?m)^WorkingDirectory=([^\r\n]+)$', text) == [old], 'WorkingDirectory pin'
assert old in text and text.count(old) >= 2, 'command must reference the baseline release'
out = re.sub(re.escape(old) + r'(?=$|[/\s"\x27])', lambda _: new, text, flags=re.M)
assert out != text and old not in out and new in out
Path(dst).write_text(out, encoding='utf-8')
PY
done
printf 'prepared\n' > state
find . -maxdepth 1 -type f ! -name digests -printf '%P\n' | sort | while read -r f; do sha256sum "$f"; done > digests
sha256sum -c digests > /dev/null
echo "RECORD_READY $record"
