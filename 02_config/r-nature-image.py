#!/usr/bin/env python3
"""Build the pinned Nature R Apptainer derivative from local inputs only."""
import argparse
from hashlib import file_digest
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

LOCK = Path(__file__).resolve().with_name('r-nature-runtime.lock.json')
PREFIX = '/opt/citrus-r'
RECIPE = """Bootstrap: localimage
From: {base}

%files
    {stage} /opt/citrus-r-build

%labels
    CitrusNatureRLockSHA256 {digest}

%post
    set -eu
    build=/opt/citrus-r-build
    export MAMBA_ROOT_PREFIX=/opt/citrus-r-build/mamba-root
    export MAMBA_NO_BANNER=1
    "$build/micromamba" --no-rc --offline create --yes --always-copy -p /opt/citrus-r -f "$build/packages.txt"
    printf '%s\\n' '{digest}' > /opt/citrus-r/citrus-runtime-lock.sha256
    rm -rf -- "$build"

%test
    set -eu
    python -I -m pip --isolated check
    /opt/citrus-r/bin/Rscript --vanilla -e 'p <- c(ggplot2="4.0.4",patchwork="1.3.2",svglite="2.2.2",ragg="1.5.2",jsonlite="2.0.0"); stopifnot(getRversion() == "4.4.3", capabilities("cairo"), all(vapply(names(p), function(x) as.character(packageVersion(x)), "") == p))'
"""


def sha256(path):
    with path.open('rb') as stream:
        return file_digest(stream, 'sha256').hexdigest()


def package_source(root, entry):
    filename = entry['filename']
    if not re.fullmatch(r'[A-Za-z0-9_.+\-]+\.(?:conda|tar\.bz2)', filename):
        raise ValueError('Invalid package filename')
    match = re.fullmatch(r'https://conda\.anaconda\.org/(?:conda-forge|bioconda)/(linux-64|noarch)/([^/]+)', entry['url'])
    if not match or match[2] != filename:
        raise ValueError('Invalid package URL')
    flat = root / filename
    return flat if flat.is_file() else root / 'https' / entry['url'].removeprefix('https://')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--micromamba', type=Path, required=True)
    parser.add_argument('--packages', type=Path, required=True, help='Flat archive directory or micromamba pkgs cache')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    lock = json.loads(LOCK.read_bytes())
    image = args.base.resolve(strict=True)
    target = args.output.absolute()
    if not re.fullmatch(r'/[A-Za-z0-9_./+-]+\.sif', str(image)) or target.exists() or target.suffix != '.sif':
        parser.error('Provide a trusted local base SIF and new output SIF')
    if sha256(image) != lock['base_sif_sha256'] or sha256(args.micromamba) != lock['micromamba_sha256']:
        raise ValueError('Base SIF or micromamba digest mismatch')
    digest = sha256(LOCK)
    with tempfile.TemporaryDirectory(prefix='citrus-r-image-') as area:
        work = Path(area)
        stage = work / 'build-input'
        stage.mkdir()
        tool = stage / 'micromamba'
        shutil.copyfile(args.micromamba, tool)
        if sha256(tool) != lock['micromamba_sha256']:
            raise ValueError('micromamba changed while being copied')
        tool.chmod(0o755)
        lines = ['@EXPLICIT']
        for entry in lock['packages']:
            name = entry['filename']
            package = package_source(args.packages, entry)
            copied = stage / name
            if copied.exists() or not re.fullmatch(r'[0-9a-f]{32}', entry['md5']):
                raise ValueError('Duplicate or invalid locked package')
            shutil.copyfile(package, copied)
            if copied.stat().st_size != entry['size'] or sha256(copied) != entry['sha256']:
                raise ValueError(f'Package digest/size mismatch: {name}')
            lines.append(f'file:///opt/citrus-r-build/{name}#{entry["md5"]}')
        (stage / 'packages.txt').write_text('\n'.join(lines) + '\n', encoding='ascii')
        definition = work / 'nature-r.def'
        definition.write_text(RECIPE.format(base=image, stage=stage, digest=digest), encoding='ascii')
        # Avoid inherited proxy/config overrides and explicitly disable image caching.
        env = {'PATH':'/usr/bin:/bin','HOME':str(Path.home()),'LANG':'C.UTF-8',
               'APPTAINER_TMPDIR':str(work),'APPTAINER_CACHEDIR':str(work / 'cache')}
        subprocess.run(['/usr/bin/apptainer', 'build', '--disable-cache', str(target), str(definition)],
                       check=True, env=env)
        target.chmod(0o500)
    print(f'{sha256(target)}  {target}')


if __name__ == '__main__':
    main()
