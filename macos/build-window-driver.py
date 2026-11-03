#!/usr/bin/env python3
"""Build the Wine window and Dock corrections from pinned public inputs.
SPDX-License-Identifier: GPL-3.0-only
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / 'installers'))
from build_runtime import BUILD, CACHE
from bridge_setup.core import CATALOG
from bridge_setup.util import digest, download, extract


def run(args, **kwargs):
    subprocess.run([str(x) for x in args], check=True, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'darwin':
        parser.error('The Wine Mac driver must be built on macOS')
    BUILD.mkdir(parents=True, exist_ok=True)
    source_archive = download(CATALOG['wine-source'], CACHE)
    patches = [REPO / 'macos/wine-patches' / name for name in (
        '0001-preserve-exitlag-cropped-window-frame.patch',
        '0002-use-exitlag-transparent-dock-icon.patch')]
    stock = download(CATALOG['wine-macos'], CACHE)
    with tarfile.open(stock) as archive:
        candidates = [m for m in archive if m.isfile() and m.name.endswith('/lib/wine/x86_64-unix/winemac.so')]
        if len(candidates) != 1:
            raise RuntimeError('Unexpected pinned Mac runtime driver layout')
        original_hash = hashlib.sha256(archive.extractfile(candidates[0]).read()).hexdigest()
    work = BUILD / 'mac-window-driver'
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    # A mutable development cache may contain local files. Release source is
    # extracted afresh from its verified archive before applying either patch.
    source_base = work / 'source'
    extract(source_archive, source_base)
    copied = next(p for p in source_base.iterdir() if (p / 'configure').exists())
    run(['patch', '-p1', '-i', REPO / 'wine-wbemprox/patches/0001-wbemprox-add-logicaldisk-root-directory.patch'], cwd=copied)
    for patch in patches:
        run(['patch', '-p1', '-i', patch], cwd=copied)
    env = os.environ.copy()
    tools = BUILD / 'host-tools/bin'
    env['PATH'] = str(tools) + os.pathsep + env['PATH']
    bison = shutil.which('bison', path=env['PATH'])
    if not bison or ' 3.' not in subprocess.check_output([bison, '--version'], text=True).splitlines()[0]:
        raise RuntimeError('Build the pinned Windows payloads first to prepare Bison 3')
    mapping = '-ffile-prefix-map=' + str(REPO) + '=/build/exitlag-bridge'
    env.update(CC='clang -arch x86_64', CXX='clang++ -arch x86_64', OBJC='clang -arch x86_64',
               CFLAGS='-O2 -mmacosx-version-min=15.0 ' + mapping,
               OBJCFLAGS='-O2 -mmacosx-version-min=15.0 ' + mapping,
               LDFLAGS='-arch x86_64 -mmacosx-version-min=15.0')
    build = work / 'build'; build.mkdir()
    run([copied / 'configure', '--host=x86_64-apple-darwin', '--enable-archs=none', '--disable-tests',
         '--without-x', '--without-wayland', '--without-freetype', '--without-fontconfig'], cwd=build, env=env)
    run(['make', '-j8', 'dlls/winemac.drv/winemac.so'], cwd=build, env=env)
    args.output.mkdir(parents=True, exist_ok=True)
    binary = args.output / 'winemac.so'
    shutil.copy2(build / 'dlls/winemac.drv/winemac.so', binary)
    run(['/usr/bin/codesign', '--force', '--sign', '-', binary])
    run(['/usr/bin/codesign', '--verify', '--strict', binary])
    data = binary.read_bytes()
    for prefix in (b'/Users/', b'/home/'):
        if prefix in data or prefix.decode().encode('utf-16le') in data:
            raise RuntimeError('Compiled window driver contains an absolute home-directory path')
    manifest = dict(schema=1, project='exitlag-bridge', original_sha256=original_hash,
                    patched_sha256=digest(binary), source_commit=CATALOG['wine-source']['source_commit'],
                    patch_sha256=hashlib.sha256(b''.join(p.read_bytes() for p in patches)).hexdigest(),
                    runtime_archive_sha256=CATALOG['wine-macos']['sha256'])
    (args.output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (args.output / 'manifest.json').chmod(0o644)
    # Supply all corresponding Wine source, all patches, and the build recipes.
    def normalize(info):
        info.uid = info.gid = 0; info.uname = info.gname = 'root'; info.mtime = 0
        return info
    archive_path = BUILD / 'exitlag-bridge-macos-wine-source.tar.gz'
    with tarfile.open(archive_path, 'w:gz') as archive:
        for path in sorted(copied.rglob('*')):
            if path.is_file() and path.name != '.bridge-patched':
                archive.add(path, arcname='wine-11.17/' + str(path.relative_to(copied)), recursive=False, filter=normalize)
        for file in (Path(__file__).resolve(), REPO / 'installers/build_runtime.py', *patches,
                     REPO / 'wine-wbemprox/patches/0001-wbemprox-add-logicaldisk-root-directory.patch',
                     REPO / 'installers/bridge_setup/catalog.json'):
            archive.add(file, arcname='bridge/' + str(file.relative_to(REPO)), filter=normalize)
        for file in sorted((REPO / 'installers/bridge_setup').glob('*.py')):
            archive.add(file, arcname='bridge/' + str(file.relative_to(REPO)), filter=normalize)
    print(json.dumps(dict(window_driver_ready=True, corresponding_source=str(archive_path))))


if __name__ == '__main__':
    main()
