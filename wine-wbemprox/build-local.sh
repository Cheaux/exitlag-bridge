#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
source_dir="$repo_root/vendor/wine-11.17-src"
build_dir="$script_dir/build64"
stage_dir="$script_dir/runtime-staging"
patch_file="$script_dir/patches/0001-wbemprox-add-logicaldisk-root-directory.patch"
expected_commit="36b6a2cf679fb395f668a917b76537190e212d9c"
system_wine_root="/usr/lib/wine"
system_wbemprox="$system_wine_root/x86_64-windows/wbemprox.dll"

for command_name in git make sha256sum wine; do
    command -v "$command_name" >/dev/null
done

if [[ "$(wine --version)" != "wine-11.17 (Staging)" ]]; then
    echo "this reproducible build expects wine-11.17 (Staging)" >&2
    exit 1
fi
if [[ ! -f "$system_wbemprox" ]]; then
    echo "missing installed Wine module tree: $system_wine_root" >&2
    exit 1
fi

if [[ ! -d "$source_dir/.git" ]]; then
    git clone --branch wine-11.17 --depth 1 \
        https://gitlab.winehq.org/wine/wine.git "$source_dir"
fi
if [[ "$(git -C "$source_dir" rev-parse HEAD)" != "$expected_commit" ]]; then
    echo "Wine source is not the pinned wine-11.17 commit" >&2
    exit 1
fi

if git -C "$source_dir" apply --reverse --check "$patch_file" 2>/dev/null; then
    echo "wbemprox patch already applied"
elif git -C "$source_dir" apply --check "$patch_file"; then
    git -C "$source_dir" apply "$patch_file"
else
    echo "wbemprox source is neither clean nor exactly patched" >&2
    exit 1
fi

mkdir -p "$build_dir"
if [[ ! -f "$build_dir/Makefile" ]]; then
    (
        cd "$build_dir"
        "$source_dir/configure" --enable-win64 --without-x --without-wayland
    )
fi
make -C "$build_dir" -j"$(nproc)" dlls/wbemprox/x86_64-windows/wbemprox.dll

system_hash_before="$(sha256sum "$system_wbemprox")"
mkdir -p "$stage_dir"
cp -a -s -n "$system_wine_root/." "$stage_dir/"

# These three files must be real local copies. If they remain symlinks resolving to
# /usr/lib, Wine derives the installed module directory and bypasses the local tree.
for loader_file in ntdll.so wine wine-preloader; do
    cp --remove-destination \
        "$system_wine_root/x86_64-unix/$loader_file" \
        "$stage_dir/x86_64-unix/$loader_file"
done
cp --remove-destination \
    "$build_dir/dlls/wbemprox/x86_64-windows/wbemprox.dll" \
    "$stage_dir/x86_64-windows/wbemprox.dll"

# Relocating Wine's loader makes its compiled relative data lookup resolve here.
mkdir -p "$repo_root/share"
ln -sfn /usr/share/wine "$repo_root/share/wine"

system_hash_after="$(sha256sum "$system_wbemprox")"
if [[ "$system_hash_before" != "$system_hash_after" ]]; then
    echo "system wbemprox changed unexpectedly" >&2
    exit 1
fi

echo "local Wine runtime: $stage_dir"
echo "patched wbemprox: $stage_dir/x86_64-windows/wbemprox.dll"
echo "system module unchanged: $system_hash_after"
