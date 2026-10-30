#!/usr/bin/env python3
"""Identify the process that actually owns a game's network sockets.

    sudo tools/discover-game-processes.py               # everything with sockets
    sudo tools/discover-game-processes.py --min-port 1024 --exclude-known
    tools/discover-game-processes.py --json

A launcher is often not the process that opens the game's sockets. Tibia is the
worked example: ~/Games/Tibia/Tibia is a launcher which downloads and runs
~/.local/share/CipSoft GmbH/Tibia/packages/Tibia/bin/client, and only the latter
speaks to the game. Mapping the launcher achieves nothing.

Evidence here is socket ownership and /proc, never a guess from a command line.
There is deliberately no ".exe appears in argv" heuristic: that was removed for
being spoofable, and is not coming back.

Runs read-only. Root sees every process; without it you see your own, which is
usually enough since games run as you.
"""
import argparse, json, os, re, sys
from pathlib import Path

PROC = Path("/proc")
# Sockets almost every desktop opens; not games, and noisy.
INFRA_PORTS = {53, 67, 68, 123, 631, 5353}
# Shells, init and session plumbing are never a meaningful "launcher": every
# process has one somewhere up the tree.
NOT_A_LAUNCHER = re.compile(
    r"^/(usr/)?(bin|sbin)/(bash|sh|zsh|dash|fish|env|systemd.*|login|su|sudo|setsid|"
    r"nohup|strace|time)$|/systemd$|/init$")
INFRA_EXE = re.compile(
    r"/(systemd|dbus|NetworkManager|pipewire|wireplumber|pulseaudio|chrome|firefox|"
    r"thunderbird|Discord|code|steamwebhelper|gnome-|kde|plasma|Xwayland|sshd)")


def read(p, binary=False):
    try:
        return p.read_bytes() if binary else p.read_text(errors="replace")
    except OSError:
        return b"" if binary else ""


def socket_inodes():
    """inode -> (proto, local, remote) from the kernel's own tables."""
    out = {}
    for proto, path in (("tcp", "tcp"), ("tcp", "tcp6"), ("udp", "udp"), ("udp", "udp6")):
        f = PROC / "net" / path
        try:
            lines = f.read_text(errors="replace").splitlines()[1:]
        except OSError:
            continue
        v6 = path.endswith("6")
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            try:
                inode = int(parts[9])
            except ValueError:
                continue
            if inode:
                out[inode] = (proto, fmt_addr(parts[1], v6), fmt_addr(parts[2], v6))
    return out


def fmt_addr(hexpair, v6):
    """'0100007F:1F90' -> '127.0.0.1:8080'. /proc prints 32-bit words little-endian."""
    try:
        addr_hex, port_hex = hexpair.rsplit(":", 1)
        port = int(port_hex, 16)
    except ValueError:
        return hexpair
    raw = bytes.fromhex(addr_hex)
    words = [raw[i:i + 4][::-1] for i in range(0, len(raw), 4)]
    raw = b"".join(words)
    import socket as S
    try:
        if v6:
            if raw[:12] == b"\x00" * 10 + b"\xff\xff":     # ::ffff:a.b.c.d
                return f"{S.inet_ntop(S.AF_INET, raw[12:])}:{port}"
            return f"[{S.inet_ntop(S.AF_INET6, raw)}]:{port}"
        return f"{S.inet_ntop(S.AF_INET, raw)}:{port}"
    except (OSError, ValueError):
        return hexpair


def proc_info(pid):
    d = PROC / str(pid)
    try:
        exe = os.readlink(d / "exe")
    except OSError:
        exe = ""
    cmd = read(d / "cmdline", binary=True).split(b"\x00")
    cmdline = [c.decode("utf-8", "replace") for c in cmd if c]
    status = read(d / "status")
    ppid = uid = None
    for line in status.splitlines():
        if line.startswith("PPid:"):
            ppid = int(line.split()[1])
        elif line.startswith("Uid:"):
            uid = int(line.split()[1])
    env = {}
    for entry in read(d / "environ", binary=True).split(b"\x00"):
        if b"=" in entry:
            k, _, v = entry.partition(b"=")
            k = k.decode("utf-8", "replace")
            if k in ("WINEPREFIX", "STEAM_COMPAT_DATA_PATH", "SteamAppId", "STEAM_COMPAT_APP_ID"):
                env[k] = v.decode("utf-8", "replace")
    return {"pid": pid, "exe": exe, "cmdline": cmdline, "ppid": ppid, "uid": uid,
            "comm": read(d / "comm").strip(), "env": env}


def sockets_by_pid(inodes):
    """Authoritative: which PID holds which socket, from /proc/<pid>/fd."""
    owned = {}
    for d in PROC.iterdir():
        if not d.name.isdigit():
            continue
        pid = int(d.name)
        try:
            fds = list((d / "fd").iterdir())
        except OSError:
            continue                                   # not ours, or gone
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if not target.startswith("socket:["):
                continue
            try:
                inode = int(target[8:-1])
            except ValueError:
                continue
            if inode in inodes:
                owned.setdefault(pid, []).append(inodes[inode])
    return owned


def classify(info):
    """native | wine | proton, from the environment rather than the path."""
    env = info["env"]
    if env.get("STEAM_COMPAT_DATA_PATH"):
        return "proton"
    if env.get("WINEPREFIX"):
        return "wine"
    return "native"


def suggested_source(info):
    """The identity-map SOURCE this process would need. None if ambiguous."""
    kind = classify(info)
    if kind == "proton":
        pfx = Path(info["env"]["STEAM_COMPAT_DATA_PATH"]) / "pfx"
        return f"wineprefix:{os.path.realpath(pfx)}" if pfx.is_dir() else None
    if kind == "wine":
        pfx = info["env"].get("WINEPREFIX")
        return f"wineprefix:{os.path.realpath(pfx)}" if pfx and Path(pfx).is_dir() else None
    return os.path.realpath(info["exe"]) if info["exe"] else None


def ancestry(pid, limit=6):
    chain = []
    seen = set()
    while pid and pid > 1 and pid not in seen and len(chain) < limit:
        seen.add(pid)
        info = proc_info(pid)
        chain.append(info)
        pid = info["ppid"]
    return chain


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true", help="include desktop infrastructure")
    ap.add_argument("--min-port", type=int, default=0, help="ignore sockets below this remote port")
    ap.add_argument("--pid", type=int, help="inspect one PID and its ancestry")
    args = ap.parse_args()

    inodes = socket_inodes()
    owned = sockets_by_pid(inodes)

    results = []
    for pid, socks in sorted(owned.items()):
        info = proc_info(pid)
        if not info["exe"]:
            continue
        if args.pid and pid != args.pid:
            continue
        if not args.all and not args.pid:
            if INFRA_EXE.search(info["exe"]):
                continue
            socks = [s for s in socks
                     if not any(p in s[2] for p in (f":{q}" for q in INFRA_PORTS))]
            if args.min_port:
                def remote_port(s):
                    try:
                        return int(s[2].rsplit(":", 1)[1])
                    except (IndexError, ValueError):
                        return 0
                socks = [s for s in socks if remote_port(s) >= args.min_port]
            if not socks:
                continue
        chain = ancestry(pid)
        # A launcher is an ancestor that is a different executable and holds no
        # game sockets of its own.
        launcher = None
        for parent in chain[1:]:
            if (parent["exe"] and parent["exe"] != info["exe"]
                    and parent["pid"] not in owned
                    and not NOT_A_LAUNCHER.search(parent["exe"])):
                launcher = parent
                break
        results.append({
            "pid": pid, "comm": info["comm"], "exe": info["exe"], "uid": info["uid"],
            "kind": classify(info), "cmdline": info["cmdline"][:4],
            "sockets": [{"proto": p, "local": l, "remote": r} for p, l, r in socks[:8]],
            "socket_count": len(socks),
            "suggested_source": suggested_source(info),
            "launcher": ({"pid": launcher["pid"], "exe": launcher["exe"]} if launcher else None),
            "wine_prefix": info["env"].get("WINEPREFIX"),
            "steam_compat_data": info["env"].get("STEAM_COMPAT_DATA_PATH"),
        })

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    if not results:
        print("No candidate network-owning processes found.")
        if os.geteuid() != 0:
            print("(not running as root: only your own processes are visible)")
        return 0

    for r in results:
        print(f"\nNetwork-owning process")
        print(f"  PID          : {r['pid']} ({r['comm']}) uid={r['uid']}")
        print(f"  executable   : {r['exe']}")
        print(f"  kind         : {r['kind']}")
        if r["launcher"]:
            print(f"  launched by  : {r['launcher']['exe']}")
            print(f"                 ^ that is the LAUNCHER and owns no game sockets;")
            print(f"                   mapping it would achieve nothing")
        if r["wine_prefix"]:
            print(f"  WINEPREFIX   : {r['wine_prefix']}")
        if r["steam_compat_data"]:
            print(f"  compatdata   : {r['steam_compat_data']}")
        protos = sorted({s["proto"] for s in r["sockets"]})
        print(f"  transport    : {'/'.join(p.upper() for p in protos) or '-'}"
              f"  ({r['socket_count']} socket(s))")
        for s in r["sockets"][:4]:
            print(f"    {s['proto']:3} {s['local']} -> {s['remote']}")
        print(f"  suggested source:")
        print(f"    {r['suggested_source'] or '(could not determine - inspect manually)'}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
