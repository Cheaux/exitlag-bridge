#!/usr/bin/env python3
"""Turn a running game into an identity-map entry, with the Windows side confirmed.

    sudo tools/identity-map-assistant.py                 # inspect and suggest
    sudo tools/identity-map-assistant.py --append games.map
    tools/identity-map-assistant.py --json

The Linux side of a mapping can be determined automatically: which process owns
the game's sockets, whether it is native, Wine or Proton, and therefore what the
SOURCE must be. The Windows side cannot. ExitLag matches against the image path
its own catalogue expects, and guessing that would produce a mapping that looks
right and silently never matches.

So this never invents a Windows identity. It proposes candidates, marks them as
requiring confirmation, and refuses to write an entry until one is chosen.
"""
import argparse, json, os, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DISCOVER = HERE / "discover-game-processes.py"


def discover(min_port):
    out = subprocess.run([sys.executable, str(DISCOVER), "--json", "--min-port", str(min_port)],
                         capture_output=True, text=True)
    if out.returncode != 0:
        print(out.stderr.strip(), file=sys.stderr)
        return []
    try:
        return json.loads(out.stdout or "[]")
    except json.JSONDecodeError:
        return []


def windows_candidates(proc):
    """Plausible Windows image paths, each needing confirmation.

    Candidates come from what is actually on disk -- a Windows install inside
    the same Wine/Proton prefix -- or from the executable's own name. None is
    authoritative: only ExitLag knows what its catalogue expects.
    """
    cands = []
    exe = Path(proc["exe"]) if proc["exe"] else None

    prefix = None
    if proc.get("steam_compat_data"):
        prefix = Path(proc["steam_compat_data"]) / "pfx"
    elif proc.get("wine_prefix"):
        prefix = Path(proc["wine_prefix"])

    if prefix and prefix.is_dir():
        # Real .exe files inside the prefix are the strongest evidence we have.
        drive_c = prefix / "drive_c"
        if drive_c.is_dir():
            found = []
            for root, dirs, files in os.walk(drive_c):
                depth = len(Path(root).relative_to(drive_c).parts)
                if depth > 4:
                    dirs[:] = []
                    continue
                dirs[:] = [d for d in dirs if d.lower() not in ("windows", "temp")]
                for fn in files:
                    if fn.lower().endswith(".exe"):
                        rel = Path(root).relative_to(drive_c) / fn
                        found.append("C:\\" + str(rel).replace("/", "\\"))
                if len(found) > 40:
                    break
            for f in sorted(found)[:10]:
                cands.append({"path": f, "evidence": "an .exe present in this prefix"})

    if exe:
        stem = exe.name
        if not stem.lower().endswith(".exe"):
            stem += ".exe"
        cands.append({"path": f"C:\\Program Files\\<vendor>\\{stem}",
                      "evidence": "derived from the Linux executable name; almost certainly needs editing"})
    return cands


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-port", type=int, default=1024)
    ap.add_argument("--append", metavar="MAPFILE",
                    help="append confirmed entries to this map file")
    ap.add_argument("--windows-identity", metavar="PATH",
                    help="the confirmed Windows image path (required with --append)")
    ap.add_argument("--pid", type=int, help="only this PID")
    args = ap.parse_args()

    procs = [p for p in discover(args.min_port) if not args.pid or p["pid"] == args.pid]
    if not procs:
        print("No candidate game processes found.")
        print("Start the game first, then run this again.")
        if os.geteuid() != 0:
            print("(not root: only your own processes are visible)")
        return 0

    report = []
    for p in procs:
        entry = {
            "pid": p["pid"], "executable": p["exe"], "kind": p["kind"],
            "transport": sorted({s["proto"].upper() for s in p["sockets"]}),
            "endpoints": [f"{s['local']} -> {s['remote']}" for s in p["sockets"][:4]],
            "launcher": p["launcher"]["exe"] if p["launcher"] else None,
            "suggested_source": p["suggested_source"],
            "windows_identity": None,
            "windows_candidates": windows_candidates(p),
        }
        report.append(entry)

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    for e in report:
        print("\nDetected network process:")
        print(f"  PID        : {e['pid']}")
        print(f"  executable : {e['executable']}")
        print(f"  kind       : {e['kind']}")
        print(f"  transport  : {'/'.join(e['transport']) or '-'}")
        for ep in e["endpoints"]:
            print(f"  endpoint   : {ep}")
        if e["launcher"]:
            print(f"  launcher   : {e['launcher']}")
            print( "               (not the socket owner -- do not map this)")
        print("\nSuggested source:")
        print(f"  {e['suggested_source'] or '(undetermined -- inspect manually)'}")
        print("\nWindows identity:")
        print("  <needs selection/confirmation -- not invented>")
        if e["windows_candidates"]:
            print("  candidates:")
            for c in e["windows_candidates"][:6]:
                print(f"    {c['path']}")
                print(f"        ({c['evidence']})")
        print("\n  Confirm with:")
        print(f"    tools/identity-map-assistant.py --pid {e['pid']} \\")
        print( "        --windows-identity 'C:\\...\\game.exe' --append runtime/games.map")

    if args.append:
        if not args.windows_identity:
            print("\nRefusing to write: --append requires --windows-identity.", file=sys.stderr)
            print("The Windows side must be confirmed by a human; it is never inferred.",
                  file=sys.stderr)
            return 2
        if len(report) != 1:
            print(f"\nRefusing to write: {len(report)} processes matched. Use --pid to pick one.",
                  file=sys.stderr)
            return 2
        src = report[0]["suggested_source"]
        if not src:
            print("\nRefusing to write: no source could be determined.", file=sys.stderr)
            return 2
        line = f"{src}\t{args.windows_identity}\n"
        path = Path(args.append)
        existing = path.read_text() if path.exists() else ""
        if line in existing:
            print(f"\nAlready present in {path}; nothing to do.")
            return 0
        with path.open("a") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(f"# added by identity-map-assistant for pid {report[0]['pid']}\n")
            fh.write(line)
        print(f"\nAppended to {path}:")
        print(f"  {src}")
        print(f"      -> {args.windows_identity}")
        print("\nVerify it loads:  backend/exitlag-netd --no-queue --identity-map "
              f"{path} --token-file /tmp/t --log /tmp/l && tail /tmp/l")
    return 0


if __name__ == "__main__":
    sys.exit(main())
