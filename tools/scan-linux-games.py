#!/usr/bin/env python3
"""Generate identity-map candidates for locally installed games.

    tools/scan-linux-games.py                 # print candidates
    tools/scan-linux-games.py -o games.map    # write a reviewable map file

This is a GENERATOR, not a matcher. It proposes exact lines for you to check;
the backend still matches exactly at runtime, with no fuzzy or partial matching
anywhere. Every proposed line is emitted commented-out so nothing is routed
until a human has looked at it and uncommented it.

Why this exists: ExitLag detects games with Process32NextW over its own Wine
prefix, looking for a Windows executable. A native Linux game has neither, so
the client cannot see it -- but it can still be routed, because the routing
decision is made per-connection from the identity the backend reports.
"""
import argparse, os, re, struct, sys
from pathlib import Path

STEAM_DIRS = [Path.home()/".local/share/Steam", Path.home()/".steam/steam",
              Path.home()/".var/app/com.valvesoftware.Steam/.local/share/Steam"]
# Where Steam lives on Windows, which is what ExitLag's catalogue expects.
WIN_STEAM = r"C:\Program Files (x86)\Steam\steamapps\common"


def steam_root():
    for d in STEAM_DIRS:
        if (d/"steamapps").is_dir():
            return d
    return None


def library_paths(root):
    libs = [root]
    vdf = root/"steamapps/libraryfolders.vdf"
    if vdf.exists():
        for m in re.finditer(r'"path"\s*"([^"]+)"', vdf.read_text(errors="replace")):
            p = Path(m.group(1))
            if p.is_dir() and p not in libs:
                libs.append(p)
    return libs


def installed_apps(lib):
    out = []
    d = lib/"steamapps"
    for acf in sorted(d.glob("appmanifest_*.acf")):
        t = acf.read_text(errors="replace")
        def field(k):
            m = re.search(r'"%s"\s*"([^"]*)"' % k, t)
            return m.group(1) if m else None
        appid, name, installdir = field("appid"), field("name"), field("installdir")
        skip = ("proton" in (name or installdir).lower()
                or "steam linux runtime" in (name or "").lower()
                or "redistributables" in (name or "").lower()
                or "steamworks" in (name or "").lower())
        if appid and installdir and not skip:
            path = d/"common"/installdir
            if path.is_dir():
                out.append((int(appid), name or installdir, installdir, path))
    return out


def is_elf(p):
    try:
        with open(p, "rb") as f:
            return f.read(4) == b"\x7fELF"
    except OSError:
        return False


# libfoo.so, libfoo.so.6, libfoo.so.6.9.0 -- all libraries, none of them a game.
SO_RE = re.compile(r"\.so(\.\d+)*$")


def native_binaries(root, installdir):
    """Candidate game executables, best guess first.

    Size alone is a bad signal: a game's shipped copies of libavcodec and
    libfreetype are larger than the game binary. Rank by how much a file looks
    like the thing the launcher runs instead.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d.lower() not in
                       ("shaders", "cache", "logs", "temp", "screenshots", "var")]
        for fn in filenames:
            p = Path(dirpath)/fn
            low = fn.lower()
            if SO_RE.search(low) or low.startswith("lib"):
                continue
            if p.suffix.lower() in (".dll", ".exe", ".dat", ".vpk", ".pak", ".bin"):
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if st.st_size < 64*1024 or not os.access(p, os.X_OK) or not is_elf(p):
                continue
            stem = re.sub(r"[^a-z0-9]", "", low)
            want = re.sub(r"[^a-z0-9]", "", installdir.lower())
            score = 0
            if stem and (stem in want or want in stem):
                score += 100          # named like the game
            if "bin" in [c.lower() for c in p.relative_to(root).parts[:-1]]:
                score += 50           # lives in a bin/ directory
            if len(p.relative_to(root).parts) <= 2:
                score += 20           # at the install root
            score += min(st.st_size // (1024*1024), 20)
            found.append((score, st.st_size, p))
    found.sort(key=lambda t: (-t[0], -t[1]))
    return [p for _, _, p in found]


def has_windows_exe(root):
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            if fn.lower().endswith(".exe"):
                return Path(dirpath)/fn
    return None


def compatdata_prefix(lib, appid):
    p = lib/"steamapps/compatdata"/str(appid)/"pfx"
    return p if p.is_dir() else None


LINUX_TO_WIN_DIR = {"linuxsteamrt64": "win64", "linux64": "win64",
                    "linuxsteamrt32": "win32", "linux32": "win32", "linux": "win64"}


def windows_path_for(installdir, rel_parts, exe_name):
    """Best guess at the path ExitLag's catalogue expects.

    Valve games keep a parallel layout (bin/linuxsteamrt64 <-> bin/win64), so
    translating the directory names gets much closer than a bare basename.
    """
    parts = [LINUX_TO_WIN_DIR.get(p.lower(), p) for p in rel_parts]
    tail = "\\".join(parts + [exe_name]) if parts else exe_name
    return "%s\\%s\\%s" % (WIN_STEAM, installdir, tail)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", help="write the map file here")
    args = ap.parse_args()

    root = steam_root()
    if not root:
        print("no Steam installation found in: %s" % ", ".join(str(d) for d in STEAM_DIRS),
              file=sys.stderr)
        return 2

    lines = [
        "# Identity-map candidates generated by tools/scan-linux-games.py",
        "# Format: SOURCE<TAB>WINDOWS_PATH   (one literal tab)",
        "#",
        "# EVERY LINE IS COMMENTED OUT. Uncomment only the games you want routed,",
        "# and only after checking the game is in ExitLag's supported list.",
        "#",
        "# The WINDOWS_PATH is a best guess at what ExitLag's catalogue expects. If a",
        "# game is not routed, that string is the first thing to correct.",
        "",
    ]
    native_count = proton_count = 0

    for lib in library_paths(root):
        for appid, name, installdir, path in installed_apps(lib):
            pfx = compatdata_prefix(lib, appid)
            win_exe = has_windows_exe(path)
            lines.append("# --- %s  (appid %d)" % (name, appid))
            if pfx:
                # A Proton game: the socket belongs to that prefix's wineserver,
                # so the prefix is the only thing that identifies it.
                guess = win_exe.name if win_exe else (installdir + ".exe")
                rel = list(win_exe.relative_to(path).parts[:-1]) if win_exe else []
                lines.append("#   runs under Proton")
                lines.append("# wineprefix:%s\t%s" % (pfx, windows_path_for(installdir, rel, guess)))
                proton_count += 1
            else:
                bins = native_binaries(path, installdir)
                if not bins:
                    lines.append("#   no native executable found; skipped")
                elif win_exe and not bins:
                    lines.append("#   Windows-only install with no Proton prefix; run it once first")
                else:
                    lines.append("#   native Linux build (ExitLag cannot detect this by itself)")
                    for b in bins[:3]:
                        rel = list(b.relative_to(path).parts[:-1])
                        guess = win_exe.name if win_exe else b.name + ".exe"
                        lines.append("# %s\t%s" % (b, windows_path_for(installdir, rel, guess)))
                    if len(bins) > 1:
                        lines.append("#   ^ more than one candidate: keep the one the game actually runs")
                    native_count += 1
            lines.append("")

    text = "\n".join(lines)
    if args.output:
        Path(args.output).write_text(text)
        print("wrote %s" % args.output)
    else:
        print(text)
    print("# %d native Linux game(s), %d Proton prefix(es)" % (native_count, proton_count),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
