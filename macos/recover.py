#!/usr/bin/env python3
"""Recover only an orphaned ExitLag test anchor and its known runtime files."""
import argparse
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import time


def validate_state(state, uid):
    if type(state.get("pid")) is not int or not 1 < state["pid"] < 2**31:
        raise ValueError("Invalid session owner PID")
    expected = f"com.apple/cheaux.exitlag.{uid}.{state['pid']}"
    if state.get("uid") != uid or state.get("anchor") != expected:
        raise ValueError("Refusing recovery of an unrelated PF anchor")
    return expected


def pid_exists(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--uid", required=True, type=int)
    args = parser.parse_args()
    if os.geteuid() != 0 or not 500 <= args.uid < 2**31:
        parser.error("Recovery requires administrator access and the game user's UID")
    if "SUDO_UID" in os.environ and int(os.environ["SUDO_UID"]) != args.uid:
        parser.error("The session must belong to the invoking user")
    runtime = Path("/var/run") / f"exitlag-macos-{args.uid}"
    if not runtime.exists():
        print("No stale routing session remains.")
        return
    info = runtime.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise ValueError("Unexpected routing runtime directory")
    state_path = runtime / "state.json"
    fd = os.open(state_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != args.uid
                or info.st_size > 4096):
            raise ValueError("Unexpected routing state file")
        state = json.loads(source.read(4097))
    anchor = validate_state(state, args.uid)
    if pid_exists(state["pid"]):
        raise ValueError("The supervisor is still running; recovery will not terminate a live owner")

    # Stop an orphaned copy of this exact bridge before removing its anchor.
    # Its original token path and --pf distinguish it from every other process.
    script = str(Path(__file__).resolve().parent / "bridge.py")
    listing = subprocess.check_output(["/bin/ps", "-axo", "pid=,uid=,args="], text=True)
    for line in listing.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) != 3 or not parts[0].isdigit() or parts[1] != "0":
            continue
        pid, _, command = parts
        if (f" {script} " not in " " + command + " "
                or f"--token-file {runtime / 'session.token'}" not in command
                or " --pf " not in " " + command + " "):
            continue
        os.kill(int(pid), signal.SIGTERM)
        deadline = time.monotonic() + 5
        while pid_exists(int(pid)) and time.monotonic() < deadline:
            time.sleep(.1)
        if pid_exists(int(pid)):
            os.kill(int(pid), signal.SIGKILL)

    subprocess.run(["/sbin/pfctl", "-a", anchor, "-F", "all"],
                   check=True, capture_output=True, timeout=8)
    lease = runtime / "pf-enable.token"
    if lease.exists():
        fd = os.open(lease, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "r") as source:
            info = os.fstat(source.fileno())
            value = source.read(64).strip()
            if info.st_uid != 0 or not stat.S_ISREG(info.st_mode) or not re.fullmatch(r"\d{1,40}", value):
                raise ValueError("Unexpected PF reference file")
        subprocess.run(["/sbin/pfctl", "-X", value], check=True,
                       capture_output=True, timeout=8)
    for name in ("session.token", "anchor.conf", "control", "stats.json", "state.json", "pf-enable.token"):
        (runtime / name).unlink(missing_ok=True)
    runtime.rmdir()
    print("Removed the orphaned ExitLag test anchor and runtime. Other PF rules are unchanged.")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise SystemExit(str(error))
