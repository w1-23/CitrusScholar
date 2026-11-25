"""Public release gate fixtures: no private images, runtime state or credentials."""
from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest

import stage_8043_release as gate

LAUNCHER = '''import json
import sys


def absolute(path):
    return path


def command(config, role):
    return ['apptainer', 'exec', config['app_sif']]


def verify_image(path, expected):
    assert path.endswith('.sif') and len(expected) == 64


def main():
    index = sys.argv.index('--config')
    with open(sys.argv[index + 1]) as stream:
        print(json.dumps(command(json.load(stream), sys.argv[sys.argv.index('--role') + 1])))
'''


def make_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding='utf-8')
    return path


def make_skill(root, *, active=False):
    make_file(root / 'skills/example/SKILL.md', '# Fixture')
    if active:
        make_file(root / 'fusion/bundle.json', '{}')
        for index in range(14):
            for prefix in (gate.ALIAS_ROOT, gate.ALIAS_COPY):
                make_file(root / prefix / f'core/{index}.md', str(index))
    gate.write_object(root / 'inventory.json', gate.hashes(gate.inventory(root)))
    return gate.sha(root / 'inventory.json')


class ReleaseGateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo'
        gate.git(self.root, 'init', '-q', str(self.repo))
        gate.git(self.repo, 'config', 'user.email', 'fixture@example.test')
        gate.git(self.repo, 'config', 'user.name', 'Fixture')
        make_file(self.repo / gate.SOURCE_MANIFEST, '{"stale":true}\n')
        make_file(self.repo / '01_src/scripts/apptainer_launch.py', LAUNCHER).chmod(0o700)
        baseline = self.commit('baseline')
        old = self.root / 'old-release'
        (old / 'source').mkdir(parents=True)
        tree, members = gate.archive_commit(self.repo, baseline, old / 'source.tar', old / 'source')
        members.pop(gate.SOURCE_MANIFEST)
        (old / 'source' / gate.SOURCE_MANIFEST).unlink()
        manifest = gate.source_overlay(baseline, tree, baseline, gate.hashes(members))
        gate.write_object(old / 'source' / gate.SOURCE_MANIFEST, manifest)
        gate.write_object(old / 'source-sha256.json', {'commit': baseline, 'files': gate.hashes(gate.inventory(old / 'source'))})
        make_file(old / 'launch.py', '# exact rollback file\n')
        config = {'source_head': baseline, 'source_root': str(old / 'source'), 'port': 8043}
        for label in gate.IMAGES:
            path = make_file(self.root / 'old-images' / f'{label}.sif', label)
            config[f'{label}_sif'] = str(path)
            config[f'{label}_sha256'] = gate.sha(path)
        for key in ('runtime_root', 'code_staging', 'model_origin', 'model_config_root'):
            path = self.root / key
            path.mkdir()
            config[key] = str(path)
        for name in gate.MODEL_FILES:
            make_file(Path(config['model_origin']) / name, 'Synthetic fixture. Do not copy to release.')
        historic, previous = self.root / 'historical', self.root / 'previous-active'
        config['historical_skill_releases'] = [{'path': str(historic), 'mount_path': str(self.root / 'original-mount'),
                                               'sha256': make_skill(historic)}]
        config['skill_release'] = str(previous)
        config['skill_release_sha256'] = make_skill(previous)
        self.baseline = config
        gate.write_object(old / 'deployment.json', config)
        candidate = make_file(self.repo / 'tracked.txt', 'exact committed bytes\n')
        final = self.commit('candidate')
        candidate.write_text('dirty checkout bytes\n')
        make_file(self.repo / 'untracked.txt', 'do not release\n')
        sandbox = make_file(self.root / 'new-sandbox.sif', 'synthetic derivative')
        skills = self.root / 'new-skills'
        self.args = SimpleNamespace(repository=str(self.repo), commit=final,
                                    baseline_deployment=str(old / 'deployment.json'),
                                    sandbox_sif=str(sandbox), sandbox_sha256=gate.sha(sandbox),
                                    skill_release=str(skills), skill_release_sha256=make_skill(skills, active=True),
                                    output=str(self.root / 'candidate-release'))
        self.release = Path(self.args.output)

    def tearDown(self):
        if self.release.exists():
            self.release.chmod(0o700)
            for path in self.release.rglob('*'):
                if path.is_dir():
                    path.chmod(0o700)

    def commit(self, message):
        gate.git(self.repo, 'add', '-A')
        gate.git(self.repo, 'commit', '-qm', message)
        return gate.git(self.repo, 'rev-parse', 'HEAD').decode().strip()

    def test_whole_archive_runtime_overlay_and_original_mounts(self):
        evidence = gate.stage(self.args)
        self.assertEqual(evidence, gate.verify(self.args.output, self.args.repository, evidence['manifest_sha256']))
        self.assertEqual(gate.sha(self.release / 'source.tar'), evidence['source_archive_sha256'])
        with tarfile.open(self.release / 'source.tar') as tar:
            self.assertEqual(tar.extractfile(gate.SOURCE_MANIFEST).read(), b'{"stale":true}\n')
        source = self.release / 'source'
        self.assertEqual((source / 'tracked.txt').read_text(), 'exact committed bytes\n')
        self.assertFalse((source / 'untracked.txt').exists())
        self.assertEqual(gate.read_object(source / gate.SOURCE_MANIFEST)['integrated_branch_commit'], self.args.commit)
        config = gate.read_object(self.release / 'deployment.json')
        self.assertTrue(config['external_fusion_required'])
        self.assertEqual(config['runtime_root'], self.baseline['runtime_root'])
        self.assertEqual(config['code_staging'], self.baseline['code_staging'])
        preserved = [*self.baseline['historical_skill_releases'], {
            'path': self.baseline['skill_release'], 'mount_path': self.baseline['skill_release'],
            'sha256': self.baseline['skill_release_sha256']}]
        self.assertEqual(config['historical_skill_releases'], preserved)
        self.assertEqual(Path(config['skill_release']).parent, self.release)
        for label in gate.IMAGES:
            self.assertEqual(Path(config[f'{label}_sif']).parent.parent, self.release)
        self.assertNotIn('Synthetic fixture', (self.release / 'deployment.json').read_text())
        with self.assertRaises(FileExistsError):
            gate.stage(self.args)
        self.assertEqual(gate.sha(self.release / 'release.json'), evidence['manifest_sha256'])

    def test_wrapper_preflight_and_exact_read_only_web_model_binds(self):
        gate.stage(self.args)
        for role in ('web', 'worker'):
            command = [sys.executable, str(self.release / 'launch.py'), role]
            preflight = subprocess.run([*command, '--preflight'], check=True, capture_output=True, text=True)
            self.assertIn(f'{role} command and SIF pins verified', preflight.stdout)
            argv = json.loads(subprocess.check_output([*command, '--print-command']))
            bindings = [f'{self.baseline["model_origin"]}/{name}:'
                        f'{self.baseline["model_config_root"]}/state/{name}:ro' for name in gate.MODEL_FILES]
            self.assertEqual(argv[2:-1], ['--bind', bindings[1], '--bind', bindings[0]] if role == 'web' else [])

    def test_fails_closed_when_whole_archive_bytes_change(self):
        evidence = gate.stage(self.args)
        archive = self.release / 'source.tar'
        archive.chmod(0o600)
        with archive.open('ab') as stream:
            stream.write(b'extra makes this hash different without changing tar listing')
        archive.chmod(0o400)
        with self.assertRaisesRegex(ValueError, 'SHA-256 mismatch'):
            gate.verify(self.args.output, manifest_pin=evidence['manifest_sha256'])

    def test_fails_closed_when_extracted_source_changes(self):
        gate.stage(self.args)
        path = self.release / 'source/tracked.txt'
        path.chmod(0o600)
        path.write_text('modified in place\n')
        path.chmod(0o400)
        with self.assertRaisesRegex(ValueError, 'source inventory mismatch'):
            gate.verify(self.args.output)

    def test_fails_closed_when_alias_is_resealed_with_different_bytes(self):
        root = Path(self.args.skill_release)
        (root / gate.ALIAS_COPY / 'core/0.md').write_text('different')
        (root / 'inventory.json').unlink()
        gate.write_object(root / 'inventory.json', gate.hashes(gate.inventory(root)))
        self.args.skill_release_sha256 = gate.sha(root / 'inventory.json')
        with self.assertRaisesRegex(ValueError, 'aliases must match'):
            gate.stage(self.args)
        self.assertFalse(self.release.exists())

    def test_fails_closed_for_runtime_overlap_and_short_commit(self):
        self.args.output = f'{self.baseline["runtime_root"]}/unexpected-release'
        with self.assertRaisesRegex(ValueError, 'Output overlaps'):
            gate.stage(self.args)
        self.args.commit = self.args.commit[:7]
        with self.assertRaisesRegex(ValueError, '40-digit hash'):
            gate.stage(self.args)

    def test_rejects_archive_traversal_and_nonregular_git_entries(self):
        bad = self.root / 'bad.tar'
        with tarfile.open(bad, 'w', format=tarfile.PAX_FORMAT, pax_headers={'comment': self.args.commit}) as tar:
            member = tarfile.TarInfo('../escaped')
            member.size = 1
            tar.addfile(member, io.BytesIO(b'x'))
        with self.assertRaisesRegex(ValueError, 'Invalid release member'):
            gate.archive_members(bad, self.args.commit, self.release)
        self.assertFalse((self.root / 'escaped').exists())
        (self.repo / 'bad-link').symlink_to('tracked.txt')
        with self.assertRaisesRegex(ValueError, 'non-regular Git entry'):
            gate.archive_commit(self.repo, self.commit('symlink'), bad)


if __name__ == '__main__':
    unittest.main()
