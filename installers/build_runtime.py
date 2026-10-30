#!/usr/bin/env python3
"""Reproducible payload and embedded-Python preparation; never uses a user prefix.
SPDX-License-Identifier: GPL-3.0-only
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
from bridge_setup.core import CATALOG
from bridge_setup.util import digest, download, extract, private_json

REPO = Path(__file__).resolve().parents[1]
BUILD = REPO / 'build/installer'
CACHE = BUILD / 'downloads'


def run(args, **kwargs):
    subprocess.run([str(x) for x in args], check=True, **kwargs)


def tree(asset, name):
    archive = download(CATALOG[asset], CACHE)
    destination = BUILD / name
    if not destination.exists():
        extract(archive, destination)
    return destination


def zig_tool():
    explicit = os.environ.get('ZIG')
    if explicit:
        tool = Path(explicit).resolve(strict=True)
    else:
        arch = 'aarch64' if platform.machine() in ('arm64', 'aarch64') else 'x86_64'
        host = 'macos' if sys.platform == 'darwin' else 'linux'
        root = tree(f'zig-{arch}-{host}', 'zig')
        tools = list(root.glob('*/zig'))
        if len(tools) != 1:
            raise RuntimeError('Unexpected Zig archive layout')
        tool = tools[0]
    if subprocess.check_output([str(tool), 'version'], text=True).strip() != '0.17.0':
        raise RuntimeError('This build is pinned to Zig 0.17.0')
    return tool


def build_wmi(zig, output):
    source_root = tree('wine-source', 'wine-source')
    sources = [p for p in source_root.iterdir() if (p / 'configure').is_file()]
    if len(sources) != 1:
        raise RuntimeError('Unexpected Wine source layout')
    source = sources[0]
    patch = REPO / 'wine-wbemprox/patches/0001-wbemprox-add-logicaldisk-root-directory.patch'
    marker = source / '.bridge-patched'
    if not marker.exists():
        run(['patch', '-p1', '-i', patch], cwd=source)
        marker.write_text(digest(patch))
    elif marker.read_text() != digest(patch):
        raise RuntimeError('The Wine source cache has another patch revision')
    tools = BUILD / 'bin'
    tools.mkdir(parents=True, exist_ok=True)
    def wrapper(name, body):
        path = tools / name
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)
    # Paths are generated locally and never committed or packaged.
    import shlex
    quoted = shlex.quote(str(zig))
    clang = shlex.quote(shutil.which('clang') or 'clang')
    wrapper('host-cc', f'exec {clang} "$@"')
    clangxx = shlex.quote(shutil.which('clang++') or 'clang++')
    wrapper('host-c++', f'exec {clangxx} "$@"')
    wrapper('clang-cl', f'exec {clang} --driver-mode=cl "$@"')
    wrapper('x86_64-windows-clang', f'exec {clang} --target=x86_64-pc-windows-msvc -fuse-ld=lld -Wno-unused-command-line-argument "$@"')
    wrapper('x86_64-windows-clang++', f'exec {clang} --target=x86_64-pc-windows-msvc -fuse-ld=lld -Wno-unused-command-line-argument "$@"')
    for name in ('x86_64-w64-mingw32-clang', 'amd64-w64-mingw32-clang'):
        wrapper(name, f'exec {clang} --target=x86_64-pc-windows-msvc -fuse-ld=lld -Wno-unused-command-line-argument "$@"')
    for name in ('lld-link', 'x86_64-windows-lld-link', 'x86_64-pc-windows-msvc-lld-link'):
        wrapper(name, f'exec {quoted} lld-link "$@"')
    wrapper('ar', f'exec {quoted} ar "$@"')
    wrapper('ranlib', f'exec {quoted} ranlib "$@"')
    for name in ('dlltool', 'llvm-dlltool', 'x86_64-windows-dlltool', 'x86_64-w64-mingw32-dlltool'):
        wrapper(name, f'exec {quoted} dlltool "$@"')
    env = os.environ.copy()
    env['PATH'] = str(tools) + os.pathsep + env['PATH']
    bison = shutil.which('bison', path=env['PATH'])
    version = subprocess.check_output([bison, '--version'], text=True) if bison else ''
    if not version or ' 3.' not in version.splitlines()[0]:
        root = tree('bison-source', 'bison-source')
        src = next(p for p in root.iterdir() if (p / 'configure').exists())
        installed = BUILD / 'host-tools'
        if not (installed / 'bin/bison').exists():
            run([src / 'configure', '--prefix=' + str(installed)], cwd=src)
            run(['make', '-j4'], cwd=src)
            run(['make', 'install'], cwd=src)
        env['PATH'] = str(installed / 'bin') + os.pathsep + env['PATH']
    build = BUILD / 'wine-build'
    build.mkdir(exist_ok=True)
    mapping = '-ffile-prefix-map=' + str(REPO.resolve()) + '=/build/exitlag-bridge'
    configuration = build / '.bridge-compiler-v4'
    if not configuration.exists():
        # A compiler change requires fresh import libraries and PE objects.
        shutil.rmtree(build)
        build.mkdir()
        run([source / 'configure', '--enable-archs=x86_64', '--enable-tools', '--disable-tests',
             '--without-x', '--without-wayland', '--without-freetype',
             'CC=' + str(tools / 'host-cc'), 'CXX=' + str(tools / 'host-c++'),
             'x86_64_CC=' + str(tools / 'x86_64-windows-clang')], cwd=build, env=env)
        configuration.touch()
    run(['make', '-j4', 'dlls/wbemprox/x86_64-windows/wbemprox.dll',
         'x86_64_CFLAGS=-O2 ' + mapping], cwd=build, env=env)
    shutil.copy2(build / 'dlls/wbemprox/x86_64-windows/wbemprox.dll', output)
    # Corresponding source is shipped beside distributable installers.
    archive = BUILD / 'exitlag-bridge-wine-source.tar.gz'
    def normalize(info):
        info.uid = info.gid = 0
        info.uname = info.gname = 'root'
        info.mtime = 0
        return info
    with tarfile.open(archive, 'w:gz') as tar:
        for path in sorted(source.rglob('*')):
            if path.is_file() and path.name != '.bridge-patched':
                tar.add(path, arcname='wine-11.17/' + str(path.relative_to(source)), recursive=False, filter=normalize)
        tar.add(Path(__file__), arcname='bridge/build_runtime.py', filter=normalize)
        tar.add(patch, arcname='bridge/wmi.patch', filter=normalize)
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('payloads', 'python'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--arch', choices=('arm64', 'x86_64'), default='arm64' if platform.machine() in ('arm64', 'aarch64') else 'x86_64')
    args = parser.parse_args()
    BUILD.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.action == 'python':
        key = 'python-macos-' + args.arch
        root = tree(key, 'python-' + args.arch)
        source = root / 'python'
        if not (source / 'bin/python3').exists():
            raise RuntimeError('Unexpected embedded Python archive layout')
        shutil.copytree(source, args.output, dirs_exist_ok=True, symlinks=True)
        return
    args.output.mkdir(parents=True, exist_ok=True)
    zig = zig_tool()
    env = os.environ.copy(); env['ZIG'] = str(zig)
    run(['bash', REPO / 'macos/build-shim.sh'], env=env)
    run(['bash', REPO / 'macos/build-companion.sh'], env=env)
    for name in ('nfapi.dll', 'nfextlag.sys', 'WowClassic.exe'):
        shutil.copy2(REPO / 'macos/build' / name, args.output / name)
    source_archive = build_wmi(zig, args.output / 'wbemprox.dll')
    manifest = {'project': 'exitlag-bridge', 'version': '0.4.0',
                'wine_commit': CATALOG['wine-source']['source_commit'],
                'files': {name: digest(args.output / name) for name in ('nfapi.dll', 'nfextlag.sys', 'WowClassic.exe', 'wbemprox.dll')}}
    for name in manifest['files']:
        data = (args.output / name).read_bytes()
        for prefix in (b'/Users/', b'/home/'):
            if prefix in data or prefix.decode().encode('utf-16le') in data:
                raise RuntimeError('Release payload contains an absolute user-home reference: ' + name)
    private_json(args.output / 'manifest.json', manifest)
    print(json.dumps({'payloads_ready': True, 'corresponding_wine_source': str(source_archive)}))


if __name__ == '__main__':
    main()
