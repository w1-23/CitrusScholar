set -Eeuo pipefail
umask 077
r=/home/WZQ/citrusscholar; p="$r/preflight/8043-native-restore-1126"; repo="$r/work/five-repos-host"; cd "$repo"; old="$r/releases/8043-native-aad34c5a9a25"; d="$p/production-deadline-fix"
[[ $(git branch --show-current) == main-8043 && $(git rev-parse --short=12 HEAD) == aad34c5a9a25 && -z $(git diff --cached --name-only) ]]
files=(01_src/src/code_execution/execution_bridge.py 01_src/src/code_execution/execution_bridge_test.py)
diff -u <(printf '%s\n' "${files[@]}" | sort) <(git diff --name-only HEAD | sort)
sha256sum --quiet -c "$d/source.sha256"; grep -q '1233 passed' "$d/full/pytest.log"; grep -q 'pass 189' "$d/full/node.log"; grep -q 'Verified 28 preset schemas' "$d/full/tool-schema.log"; grep -q '43 passed' "$d/focused/pytest.log"
git add -- "${files[@]}"; git diff --cached --check; git commit --no-gpg-sign -m '1126恢复原生能力：修复无限轮次截止时间的原生执行'
h=$(git rev-parse HEAD); new="$r/releases/8043-native-${h:0:12}"; printf '%s\n' "$h" > "$p/execute-deadline-accepted-commit.txt"
readarray -t pins < <(python3.12 -c 'import json,sys;d=json.load(open(sys.argv[1]));print(d["sandbox_sha256"]);print(d["skill_release_sha256"])' "$old/deployment.json")
python3.12 -B 04_scripts/stage_8043_release.py stage --repository "$repo" --commit "$h" --baseline-deployment "$old/deployment.json" --sandbox-sif "$old/images/sandbox.sif" --sandbox-sha256 "${pins[0]}" --skill-release "$old/skills-release" --skill-release-sha256 "${pins[1]}" --output "$new"
pin=$(sha256sum "$new/release.json" | cut -d' ' -f1); python3.12 -B 04_scripts/stage_8043_release.py verify --release "$new" --repository "$repo" --manifest-sha256 "$pin" > "$p/execute-deadline-verified.json"
bash "$p/prepare-1126.sh" "$new" | tee "$p/execute-deadline-cutover-record.txt"; rec=$(awk '/^RECORD_READY /{print $2}' "$p/execute-deadline-cutover-record.txt"); bash "$p/activate-execute-deadline.sh" "$new" "$rec" --check; bash "$p/activate-execute-deadline.sh" "$new" "$rec" apply
