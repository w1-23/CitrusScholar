'''Exercise cutover against fake offline units and immutable release fixtures.'''
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
UNITS = tuple(f"citrusscholar-8043-{name}.service" for name in (
    "merged-web", "merged-worker", "corpus-intake", "corpus-publisher"))
TIMER = "citrusscholar-8043-watchdog.timer"
WATCH = "citrusscholar-8043-watchdog.service"
FAKE_SYSTEMCTL = '''
import json, os, sys
from pathlib import Path
root = Path(os.environ['HOME'])
state = root / 'unit-state.json'
current = json.loads(state.read_bytes())
assert sys.argv[1] == '--user'
command, *args = sys.argv[2:]
if command == 'show':
    unit, flag, prop, value = args
    assert [flag, value] == ['-p', '--value']
    print(current[unit][prop])
    raise SystemExit(0)
if command in ('stop', 'start'):
    for unit in args:
        current[unit]['ActiveState'] = 'active' if command == 'start' else 'inactive'
elif command == 'daemon-reload':
    for unit, file in json.loads((root / 'unit-files.json').read_bytes()).items():
        current[unit]['WorkingDirectory'] = next(line.split('=', 1)[1]
            for line in Path(file).read_text().splitlines() if line.startswith('WorkingDirectory='))
else:
    assert command == 'reset-failed'
state.write_text(json.dumps(current))
'''


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _dump(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def _release(root, name, commit, *, baseline=None):
    directory = root / "releases" / name
    directory.mkdir()
    (directory / "launch.py").write_text('from sys import argv\nassert argv[2:] == ["--preflight"]\n')
    conf = dict.fromkeys(("corpus_root", "code_staging", "figure_staging", "gpu_device",
                          "model_origin", "model_config_root"), "stable")
    conf.update(source_head=commit, port=8043, listen_host="127.0.0.1", external_fusion_required=True,
                runtime_root=str(root / "runtime"), isolated_corpus={"root": str(root / "runtime")},
                app_sif=str(directory / "application.sif"), skill_release=str(directory / "skills"),
                skill_release_sha256="a" * 64, historical_skill_releases=[])
    manifest = {"source_commit": commit}
    if baseline:
        previous = baseline / "deployment.json"
        config = json.loads(previous.read_bytes())
        conf["historical_skill_releases"] = [{"mount_path": config["skill_release"],
                                              "sha256": config["skill_release_sha256"]}]
        manifest["baseline"] = {"deployment": str(previous), "sha256": _sha(previous)}
    _dump(directory / "deployment.json", conf)
    manifest["artifacts"] = {key: _sha(directory / key) for key in ("deployment.json", "launch.py")}
    _dump(directory / "release.json", manifest)
    return directory


def test_cutover_mixed_phase_is_read_only_until_exact_rollback(tmp_path):
    root = tmp_path
    scripts = root / "scripts"
    unitdir = root / ".config/systemd/user"
    unitdir.mkdir(parents=True)
    for part in ("scripts", "releases", "rollbacks", "runtime", "bin"):
        (root / part).mkdir()
    harness = root / "bin/systemctl"
    harness.write_text(f"#!{sys.executable}\n" + FAKE_SYSTEMCTL)
    harness.chmod(0o700)
    script = scripts / "cutover_8043.sh"
    script.write_text((SOURCE / script.name).read_text().replace("root=/home/WZQ/citrusscholar", f"root={root}")
                      .replace("py=/usr/bin/python3.12", f"py={sys.executable}"))
    shutil.copyfile(SOURCE / "cutover_8043_record.py", scripts / "cutover_8043_record.py")
    operations = root / "rollbacks/8043-before-auto-corpus-1125"
    operations.mkdir()
    (operations / "operation.lock").touch()
    helper = operations / "helper.sh"
    helper.write_text('precheck(){ test ! -f "$HOME/deny"; }\nheartbeat(){ true; }\nready(){ true; }\n')
    index = root / "runtime/active_index.json"
    _dump(index, {"version": 1})
    native = _release(root, "8043-native-7486cad447f3", "a" * 40)
    corpus = _release(root, "8043-reliability-619180208def", "b" * 40)
    candidate = _release(root, "8043-reliability-aaaaaaa", "c" * 40, baseline=native)
    states = {TIMER: {"ActiveState": "active"}, WATCH: {"ActiveState": "inactive"}}
    files, pins = {}, {}
    for i, unit in enumerate(UNITS):
        old = native if i < 2 else corpus
        active = "active" if i != 3 else "inactive"
        config = unitdir / (f"{unit}.d/8043-worktree.conf" if i < 2 else unit)
        config.parent.mkdir(exist_ok=True)
        image_arg = f" {old}/application.sif" if i >= 2 else ""
        config.write_text(f"[Service]\nWorkingDirectory={old}\nExecStart={old}/launch.py{image_arg}\n")
        pins[unit] = {"cwd": str(old), "source_head": ("a" if i < 2 else "b") * 40,
                      "release_manifest_sha256": _sha(old / "release.json"),
                      "config_sha256": _sha(config), "active_state": active}
        files[unit] = str(config)
        states[unit] = {"WorkingDirectory": str(old), "ActiveState": active}
    state_file = root / "unit-state.json"
    _dump(state_file, states)
    _dump(root / "unit-files.json", files)
    baseline = operations / "baseline.json"
    _dump(baseline, {"schema": 1, "units": pins, "watch_state": "active",
                     "helper": {"path": str(helper), "sha256": _sha(helper)}})
    record = root / "rollbacks/8043-before-reliability-aaaaaaa"
    env = os.environ | {"HOME": str(root), "PATH": f"{harness.parent}:/usr/bin:/bin"}

    def call(mode, *, rc=0, digest=None):
        result = subprocess.run(["bash", script, mode, candidate, record, baseline, digest or _sha(baseline)],
                                env=env, text=True, capture_output=True, timeout=15)
        assert result.returncode == rc, (mode, result.stdout, result.stderr)
        return result.stdout

    assert not call("prepare", rc=1, digest="f" * 64) and not record.exists()
    assert call("prepare") == "prepared\n"
    assert "unit_phase=old" in call("check")
    changed = Path(files[UNITS[0]])
    original = changed.read_bytes()
    staged = (record / "staged" / f"{UNITS[0]}.conf").read_bytes()
    changed.write_bytes(staged)
    untouched = state_file.read_bytes()
    assert "unit_phase=mixed" in call("check") and untouched == state_file.read_bytes()
    call("apply", rc=1)
    changed.write_text("unknown\n")
    call("rollback", rc=1)
    assert changed.read_text() == "unknown\n"
    changed.write_bytes(staged)
    _dump(index, {"version": 2})
    call("rollback")
    assert changed.read_bytes() == original and json.loads(state_file.read_bytes()) == states
    assert "index=changed" in call("check") and json.loads(index.read_bytes()) == {"version": 2}
    assert "baseline already running" in call("rollback")


if __name__ == "__main__":
    from tempfile import TemporaryDirectory

    with TemporaryDirectory(prefix="citrus-cutover-test-") as scratch:
        test_cutover_mixed_phase_is_read_only_until_exact_rollback(Path(scratch))
    print("Cutover recovery fixture passed")
